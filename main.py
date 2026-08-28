import os
import re
import csv
import io
import asyncio
import logging
from datetime import datetime

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("TELEGARM_BOT_TOKEN")
)

MAX_IBANS = 50
CONCURRENCY = 8
TIMEOUT = 20

EPC_SCT_CSV = (
    "https://www.europeanpaymentscouncil.eu/"
    "sites/default/files/participants_export/"
    "sct/sct.csv"
)

EPC_SCT_INST_CSV = (
    "https://www.europeanpaymentscouncil.eu/"
    "sites/default/files/participants_export/"
    "sct_inst/sct_inst.csv"
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("IBAN-BOT")


# ============================================================
# COUNTRIES
# ============================================================

COUNTRIES = {
    "AD": "أندورا",
    "AL": "ألبانيا",
    "AT": "النمسا",
    "BA": "البوسنة والهرسك",
    "BE": "بلجيكا",
    "BG": "بلغاريا",
    "BY": "بيلاروسيا",
    "CH": "سويسرا",
    "CY": "قبرص",
    "CZ": "التشيك",
    "DE": "ألمانيا",
    "DK": "الدنمارك",
    "EE": "إستونيا",
    "ES": "إسبانيا",
    "FI": "فنلندا",
    "FO": "جزر فارو",
    "FR": "فرنسا",
    "GB": "المملكة المتحدة",
    "GE": "جورجيا",
    "GI": "جبل طارق",
    "GL": "جرينلاند",
    "GR": "اليونان",
    "HR": "كرواتيا",
    "HU": "المجر",
    "IE": "أيرلندا",
    "IS": "آيسلندا",
    "IT": "إيطاليا",
    "LI": "ليختنشتاين",
    "LT": "ليتوانيا",
    "LU": "لوكسمبورغ",
    "LV": "لاتفيا",
    "MC": "موناكو",
    "MD": "مولدوفا",
    "ME": "الجبل الأسود",
    "MK": "مقدونيا الشمالية",
    "MT": "مالطا",
    "NL": "هولندا",
    "NO": "النرويج",
    "PL": "بولندا",
    "PT": "البرتغال",
    "RO": "رومانيا",
    "RS": "صربيا",
    "SE": "السويد",
    "SI": "سلوفينيا",
    "SK": "سلوفاكيا",
    "SM": "سان مارينو",
    "TR": "تركيا",
    "UA": "أوكرانيا",
    "VA": "الفاتيكان",
    "XK": "كوسوفو",
}


# ============================================================
# IBAN LENGTHS
# ============================================================

IBAN_LENGTHS = {
    "AD": 24,
    "AL": 28,
    "AT": 20,
    "BA": 20,
    "BE": 16,
    "BG": 22,
    "BY": 28,
    "CH": 21,
    "CY": 28,
    "CZ": 24,
    "DE": 22,
    "DK": 18,
    "EE": 20,
    "ES": 24,
    "FI": 18,
    "FO": 18,
    "FR": 27,
    "GB": 22,
    "GE": 22,
    "GI": 23,
    "GL": 18,
    "GR": 27,
    "HR": 21,
    "HU": 28,
    "IE": 22,
    "IS": 26,
    "IT": 27,
    "LI": 21,
    "LT": 20,
    "LU": 20,
    "LV": 21,
    "MC": 27,
    "MD": 24,
    "ME": 22,
    "MK": 19,
    "MT": 31,
    "NL": 18,
    "NO": 15,
    "PL": 28,
    "PT": 25,
    "RO": 24,
    "RS": 22,
    "SE": 24,
    "SI": 19,
    "SK": 24,
    "SM": 27,
    "TR": 26,
    "UA": 29,
    "VA": 22,
    "XK": 20,
}


# ============================================================
# HELPERS
# ============================================================

def clean(value):
    return re.sub(
        r"[^A-Z0-9]",
        "",
        str(value).upper(),
    )


def country_name(code):
    return COUNTRIES.get(
        code,
        code,
    )


def format_iban(iban):
    return " ".join(
        iban[i:i + 4]
        for i in range(0, len(iban), 4)
    )


def mod97_valid(iban):
    """
    ISO 13616 MOD-97.
    """

    rearranged = (
        iban[4:]
        + iban[:4]
    )

    numeric = ""

    for char in rearranged:
        if char.isdigit():
            numeric += char
        else:
            numeric += str(
                ord(char) - 55
            )

    remainder = 0

    for i in range(
        0,
        len(numeric),
        7,
    ):
        remainder = int(
            str(remainder)
            + numeric[i:i + 7]
        ) % 97

    return remainder == 1


def extract_ibans(text):
    found = []

    # IBAN with spaces
    candidates = re.findall(
        r"\b[A-Za-z]{2}[0-9A-Za-z\s]{13,40}\b",
        text,
    )

    for candidate in candidates:

        iban = clean(candidate)

        if (
            len(iban) >= 15
            and re.match(
                r"^[A-Z]{2}[0-9]",
                iban,
            )
            and iban not in found
        ):
            found.append(iban)

    # fallback
    for token in text.split():

        iban = clean(token)

        if (
            len(iban) >= 15
            and re.match(
                r"^[A-Z]{2}[0-9]",
                iban,
            )
            and iban not in found
        ):
            found.append(iban)

    return found


# ============================================================
# COUNTRY-SPECIFIC IBAN PARSER
# ============================================================

def parse_structure(iban):

    country = iban[:2]
    result = {}

    # --------------------------------------------------------
    # ITALY
    # IT + 2 check + CIN + ABI + CAB + ACCOUNT
    # --------------------------------------------------------

    if country == "IT" and len(iban) == 27:

        result["cin"] = iban[4]
        result["abi"] = iban[5:10]
        result["cab"] = iban[10:15]
        result["account"] = iban[15:]

    # --------------------------------------------------------
    # GERMANY
    # DE + check + BLZ(8) + account(10)
    # --------------------------------------------------------

    elif country == "DE" and len(iban) == 22:

        result["blz"] = iban[4:12]
        result["account"] = iban[12:]

    # --------------------------------------------------------
    # AUSTRIA
    # AT + check + BLZ(5) + account(11)
    # --------------------------------------------------------

    elif country == "AT" and len(iban) == 20:

        result["blz"] = iban[4:9]
        result["account"] = iban[9:]

    # --------------------------------------------------------
    # FRANCE / MONACO
    # Bank + Branch + Account + RIB key
    # --------------------------------------------------------

    elif country in ("FR", "MC"):

        result["bank_code"] = iban[4:9]
        result["branch_code"] = iban[9:14]
        result["account"] = iban[14:25]
        result["rib_key"] = iban[25:27]

    # --------------------------------------------------------
    # SPAIN
    # Entity + Branch + Check + Account
    # --------------------------------------------------------

    elif country == "ES":

        result["bank_code"] = iban[4:8]
        result["branch_code"] = iban[8:12]
        result["account_check"] = iban[12:14]
        result["account"] = iban[14:24]

    # --------------------------------------------------------
    # PORTUGAL
    # Bank + Branch + Account + Check
    # --------------------------------------------------------

    elif country == "PT":

        result["bank_code"] = iban[4:8]
        result["branch_code"] = iban[8:12]
        result["account"] = iban[12:23]
        result["check"] = iban[23:25]

    # --------------------------------------------------------
    # BELGIUM
    # Bank code + account + check
    # --------------------------------------------------------

    elif country == "BE":

        result["bank_code"] = iban[4:7]
        result["account"] = iban[7:14]
        result["check"] = iban[14:16]

    # --------------------------------------------------------
    # NETHERLANDS
    # 4 letters bank identifier + 10 account
    # --------------------------------------------------------

    elif country == "NL":

        result["bank_code"] = iban[4:8]
        result["account"] = iban[8:18]

    # --------------------------------------------------------
    # UNITED KINGDOM
    # 4 letters bank + 6 sort code + 8 account
    # --------------------------------------------------------

    elif country == "GB":

        result["bank_code"] = iban[4:8]
        result["sort_code"] = iban[8:14]
        result["account"] = iban[14:22]

    # --------------------------------------------------------
    # IRELAND
    # 4 letters bank + 6 branch + account
    # --------------------------------------------------------

    elif country == "IE":

        result["bank_code"] = iban[4:8]
        result["branch_code"] = iban[8:14]
        result["account"] = iban[14:22]

    # --------------------------------------------------------
    # SWITZERLAND
    # 5-digit clearing + account
    # --------------------------------------------------------

    elif country == "CH":

        result["clearing"] = iban[4:9]
        result["account"] = iban[9:21]

    # --------------------------------------------------------
    # LIECHTENSTEIN
    # 5-digit clearing + account
    # --------------------------------------------------------

    elif country == "LI":

        result["clearing"] = iban[4:9]
        result["account"] = iban[9:21]

    # --------------------------------------------------------
    # LITHUANIA
    # 5-digit bank code + account
    # --------------------------------------------------------

    elif country == "LT":

        result["bank_code"] = iban[4:9]
        result["account"] = iban[9:20]

    # --------------------------------------------------------
    # LATVIA
    # 4-letter bank + account
    # --------------------------------------------------------

    elif country == "LV":

        result["bank_code"] = iban[4:8]
        result["account"] = iban[8:21]

    # --------------------------------------------------------
    # ESTONIA
    # 2 bank digits + account
    # --------------------------------------------------------

    elif country == "EE":

        result["bank_code"] = iban[4:6]
        result["account"] = iban[6:20]

    # --------------------------------------------------------
    # FINLAND
    # bank/account BBAN
    # --------------------------------------------------------

    elif country == "FI":

        result["bank_code"] = iban[4:10]
        result["account"] = iban[10:18]

    # --------------------------------------------------------
    # DENMARK
    # 4 bank + account
    # --------------------------------------------------------

    elif country == "DK":

        result["bank_code"] = iban[4:8]
        result["account"] = iban[8:18]

    # --------------------------------------------------------
    # NORWAY
    # 4 bank + account
    # --------------------------------------------------------

    elif country == "NO":

        result["bank_code"] = iban[4:8]
        result["account"] = iban[8:15]

    # --------------------------------------------------------
    # SWEDEN
    # 3 bank + account
    # --------------------------------------------------------

    elif country == "SE":

        result["bank_code"] = iban[4:7]
        result["account"] = iban[7:24]

    # --------------------------------------------------------
    # POLAND
    # 8 bank/branch + 16 account
    # --------------------------------------------------------

    elif country == "PL":

        result["bank_code"] = iban[4:12]
        result["account"] = iban[12:28]

    # --------------------------------------------------------
    # CZECH REPUBLIC
    # Prefix + bank code + account
    # --------------------------------------------------------

    elif country == "CZ":

        result["bank_prefix"] = iban[4:10]
        result["bank_code"] = iban[10:14]
        result["account"] = iban[14:24]

    # --------------------------------------------------------
    # SLOVAKIA
    # Prefix + bank code + account
    # --------------------------------------------------------

    elif country == "SK":

        result["bank_prefix"] = iban[4:10]
        result["bank_code"] = iban[10:14]
        result["account"] = iban[14:24]

    # --------------------------------------------------------
    # HUNGARY
    # bank + branch + account
    # --------------------------------------------------------

    elif country == "HU":

        result["bank_code"] = iban[4:12]
        result["branch_code"] = iban[12:16]
        result["account"] = iban[16:28]

    # --------------------------------------------------------
    # ROMANIA
    # 4 letters bank + account
    # --------------------------------------------------------

    elif country == "RO":

        result["bank_code"] = iban[4:8]
        result["account"] = iban[8:24]

    # --------------------------------------------------------
    # BULGARIA
    # 4 letters bank + branch structure
    # --------------------------------------------------------

    elif country == "BG":

        result["bank_code"] = iban[4:8]
        result["branch_code"] = iban[8:12]
        result["account"] = iban[12:22]

    # --------------------------------------------------------
    # CROATIA
    # 7 bank digits + account
    # --------------------------------------------------------

    elif country == "HR":

        result["bank_code"] = iban[4:11]
        result["account"] = iban[11:21]

    # --------------------------------------------------------
    # SLOVENIA
    # 5 bank/branch + account
    # --------------------------------------------------------

    elif country == "SI":

        result["bank_code"] = iban[4:9]
        result["account"] = iban[9:19]

    # --------------------------------------------------------
    # SERBIA
    # --------------------------------------------------------

    elif country == "RS":

        result["bank_code"] = iban[4:7]
        result["branch_code"] = iban[7:10]
        result["account"] = iban[10:22]

    # --------------------------------------------------------
    # MONTENEGRO
    # --------------------------------------------------------

    elif country == "ME":

        result["bank_code"] = iban[4:7]
        result["branch_code"] = iban[7:10]
        result["account"] = iban[10:22]

    # --------------------------------------------------------
    # NORTH MACEDONIA
    # --------------------------------------------------------

    elif country == "MK":

        result["bank_code"] = iban[4:7]
        result["branch_code"] = iban[7:10]
        result["account"] = iban[10:19]

    # --------------------------------------------------------
    # BOSNIA
    # --------------------------------------------------------

    elif country == "BA":

        result["bank_code"] = iban[4:10]
        result["account"] = iban[10:20]

    # --------------------------------------------------------
    # ALBANIA
    # --------------------------------------------------------

    elif country == "AL":

        result["bank_code"] = iban[4:12]
        result["branch_code"] = iban[12:16]
        result["account"] = iban[16:28]

    # --------------------------------------------------------
    # TURKEY
    # --------------------------------------------------------

    elif country == "TR":

        result["bank_code"] = iban[4:9]
        result["reserve"] = iban[9:10]
        result["account"] = iban[10:26]

    # --------------------------------------------------------
    # GEORGIA
    # --------------------------------------------------------

    elif country == "GE":

        result["bank_code"] = iban[4:6]
        result["account"] = iban[6:22]

    # --------------------------------------------------------
    # MOLDOVA
    # --------------------------------------------------------

    elif country == "MD":

        result["bank_code"] = iban[4:8]
        result["account"] = iban[8:24]

    # --------------------------------------------------------
    # UKRAINE
    # --------------------------------------------------------

    elif country == "UA":

        result["bank_code"] = iban[4:10]
        result["account"] = iban[10:29]

    # --------------------------------------------------------
    # KOSOVO
    # --------------------------------------------------------

    elif country == "XK":

        result["bank_code"] = iban[4:10]
        result["account"] = iban[10:20]

    # --------------------------------------------------------
    # GENERIC
    # --------------------------------------------------------

    else:

        result["bban"] = iban[4:]

    return result


# ============================================================
# EPC DATABASE
# ============================================================

class EPCDatabase:

    def __init__(self):
        self.sct = {}
        self.instant = {}
        self.updated = 0
        self.lock = asyncio.Lock()

    async def download(
        self,
        session,
        url,
    ):

        try:

            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(
                    total=60
                ),
                headers={
                    "User-Agent":
                        "Free-European-IBAN-Bot/1.0"
                },
            ) as response:

                if response.status != 200:
                    logger.warning(
                        "EPC HTTP %s",
                        response.status,
                    )
                    return []

                raw = await response.read()

                text = raw.decode(
                    "utf-8-sig",
                    errors="replace",
                )

                try:
                    dialect = csv.Sniffer().sniff(
                        text[:10000],
                        delimiters=";,"
                    )
                except Exception:
                    dialect = csv.excel

                return list(
                    csv.DictReader(
                        io.StringIO(text),
                        dialect=dialect,
                    )
                )

        except Exception as exc:

            logger.warning(
                "EPC download failed: %s",
                exc,
            )

            return []

    def index(self, rows):

        result = {}

        for row in rows:

            normalized = {}

            for key, value in row.items():

                k = re.sub(
                    r"[^A-Z0-9]",
                    "",
                    str(key).upper(),
                )

                normalized[k] = (
                    str(value).strip()
                    if value is not None
                    else ""
                )

            bic = (
                normalized.get("BIC")
                or normalized.get("BIC11")
                or normalized.get("BIC8")
            )

            if not bic:
                continue

            bic = clean(bic)

            result[bic] = normalized

        return result

    async def refresh(
        self,
        session,
    ):

        now = asyncio.get_running_loop().time()

        if (
            self.updated
            and now - self.updated < 21600
        ):
            return

        async with self.lock:

            now = asyncio.get_running_loop().time()

            if (
                self.updated
                and now - self.updated < 21600
            ):
                return

            sct_rows, instant_rows = (
                await asyncio.gather(
                    self.download(
                        session,
                        EPC_SCT_CSV,
                    ),
                    self.download(
                        session,
                        EPC_SCT_INST_CSV,
                    ),
                )
            )

            if sct_rows:
                self.sct = self.index(
                    sct_rows
                )

            if instant_rows:
                self.instant = self.index(
                    instant_rows
                )

            self.updated = now

            logger.info(
                "EPC loaded: SCT=%d / SCT Inst=%d",
                len(self.sct),
                len(self.instant),
            )

    def find_bic(
        self,
        bic,
    ):

        if not bic:
            return {}

        bic = clean(bic)
        bic8 = bic[:8]

        sct = (
            self.sct.get(bic)
            or self.sct.get(bic8)
        )

        instant = (
            self.instant.get(bic)
            or self.instant.get(bic8)
        )

        return {
            "sct": bool(sct),
            "instant": bool(instant),
            "sct_data": sct or {},
            "instant_data": instant or {},
        }


epc = EPCDatabase()


# ============================================================
# FREE BIC LOOKUP
# ============================================================

async def lookup_bic(
    session,
    semaphore,
    code,
    country,
):

    if not code:
        return {}

    # IBANTools endpoint
    urls = [
        f"https://www.ibantools.org/api/v1/swift/{code}",
        f"https://www.ibantools.org/api/v1/swift/search?q={code}",
    ]

    async with semaphore:

        for url in urls:

            try:

                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(
                        total=TIMEOUT
                    ),
                ) as response:

                    if response.status != 200:
                        continue

                    data = await response.json()

                    if isinstance(
                        data,
                        dict,
                    ):
                        return data

                    if isinstance(
                        data,
                        list,
                    ) and data:

                        if isinstance(
                            data[0],
                            dict,
                        ):
                            return data[0]

            except Exception:
                continue

    return {}


# ============================================================
# EXTRACT BIC FROM DATA
# ============================================================

def extract_bic(data):

    if not isinstance(
        data,
        dict,
    ):
        return None

    for key in (
        "bic",
        "BIC",
        "swift",
        "SWIFT",
        "swift_code",
        "bic_code",
    ):

        value = data.get(key)

        if value:
            return str(value).upper()

    return None


def extract_bank_name(data):

    if not isinstance(
        data,
        dict,
    ):
        return None

    for key in (
        "bank_name",
        "BankName",
        "name",
        "Name",
        "institution",
        "institution_name",
    ):

        value = data.get(key)

        if value:
            return str(value)

    return None


def extract_city(data):

    if not isinstance(
        data,
        dict,
    ):
        return None

    for key in (
        "city",
        "City",
        "bank_city",
    ):

        value = data.get(key)

        if value:
            return str(value)

    return None


def extract_address(data):

    if not isinstance(
        data,
        dict,
    ):
        return None

    for key in (
        "address",
        "Address",
        "bank_address",
    ):

        value = data.get(key)

        if value:
            return str(value)

    return None


# ============================================================
# VALIDATE ONE IBAN
# ============================================================

async def process_iban(
    session,
    semaphore,
    iban,
):

    country = iban[:2]

    expected_length = IBAN_LENGTHS.get(
        country
    )

    structure = parse_structure(
        iban
    )

    result = {
        "iban": iban,
        "country": country,
        "country_name": country_name(
            country
        ),
        "expected_length": expected_length,
        "actual_length": len(iban),
        "structure": structure,
        "valid_length": (
            expected_length is None
            or len(iban)
            == expected_length
        ),
        "mod97": False,
        "bic": None,
        "bank": None,
        "city": None,
        "address": None,
        "sct": None,
        "instant": None,
    }

    # MOD97
    try:
        result["mod97"] = mod97_valid(
            iban
        )
    except Exception:
        result["mod97"] = False

    # --------------------------------------------------------
    # Try to get BIC from possible bank code
    # --------------------------------------------------------

    possible_codes = []

    for key in (
        "abi",
        "blz",
        "bank_code",
        "clearing",
    ):

        value = structure.get(key)

        if value:
            possible_codes.append(
                str(value)
            )

    # Search using bank code
    for code in possible_codes:

        data = await lookup_bic(
            session,
            semaphore,
            code,
            country,
        )

        bic = extract_bic(data)

        if bic:

            result["bic"] = bic

            result["bank"] = (
                extract_bank_name(data)
            )

            result["city"] = (
                extract_city(data)
            )

            result["address"] = (
                extract_address(data)
            )

            break

    # --------------------------------------------------------
    # EPC lookup
    # --------------------------------------------------------

    epc_data = epc.find_bic(
        result["bic"]
    )

    if result["bic"]:

        sct_data = epc_data[
            "sct_data"
        ]

        instant_data = epc_data[
            "instant_data"
        ]

        if sct_data:

            result["bank"] = (
                result["bank"]
                or sct_data.get(
                    "PARTICIPANTNAME"
                )
                or sct_data.get(
                    "NAME"
                )
            )

            result["city"] = (
                result["city"]
                or sct_data.get(
                    "CITY"
                )
            )

            result["address"] = (
                result["address"]
                or sct_data.get(
                    "ADDRESS"
                )
            )

        elif instant_data:

            result["bank"] = (
                result["bank"]
                or instant_data.get(
                    "PARTICIPANTNAME"
                )
                or instant_data.get(
                    "NAME"
                )
            )

            result["city"] = (
                result["city"]
                or instant_data.get(
                    "CITY"
                )
            )

            result["address"] = (
                result["address"]
                or instant_data.get(
                    "ADDRESS"
                )
            )

    result["sct"] = epc_data[
        "sct"
    ]

    result["instant"] = epc_data[
        "instant"
    ]

    return result


# ============================================================
# FORMAT
# ============================================================

def yes_no_unknown(value):

    if value is True:
        return "✅ نعم"

    if value is False:
        return "❌ لا"

    return "⚠️ غير محدد"


def format_result(result):

    iban = result["iban"]

    country = result[
        "country"
    ]

    length_ok = result[
        "valid_length"
    ]

    mod97 = result[
        "mod97"
    ]

    technical_valid = (
        length_ok
        and mod97
    )

    if technical_valid:
        status = (
            "✅ صالح فنيًا"
        )
    else:
        status = (
            "❌ غير صالح"
        )

    lines = []

    lines.append(
        f"🔢 <code>{format_iban(iban)}</code>"
    )

    lines.append(
        f"• الحالة: <b>{status}</b>"
    )

    lines.append(
        f"• الدولة: "
        f"{result['country_name']} "
        f"(<code>{country}</code>)"
    )

    lines.append(
        f"• طول IBAN: "
        f"{result['actual_length']}"
        + (
            f"/{result['expected_length']}"
            if result[
                "expected_length"
            ]
            else ""
        )
    )

    lines.append(
        f"• MOD-97: "
        f"{'✅ صحيح' if mod97 else '❌ خطأ'}"
    )

    structure = result[
        "structure"
    ]

    if structure:

        lines.append("")
        lines.append(
            "🏦 <b>بيانات الحساب المحلية</b>"
        )

        labels = {
            "abi": "ABI",
            "cab": "CAB",
            "cin": "CIN",
            "blz": "BLZ",
            "bank_code": "Bank Code",
            "branch_code": "Branch Code",
            "sort_code": "Sort Code",
            "clearing": "Clearing",
            "bank_prefix": "Bank Prefix",
            "account_check": "Account Check",
            "rib_key": "RIB Key",
        }

        for key, label in labels.items():

            value = structure.get(
                key
            )

            if value:
                lines.append(
                    f"• {label}: "
                    f"<code>{value}</code>"
                )

    lines.append("")
    lines.append(
        "🏢 <b>معلومات البنك</b>"
    )

    lines.append(
        "• البنك: "
        + (
            result["bank"]
            or "غير متوفر"
        )
    )

    lines.append(
        "• BIC/SWIFT: "
        + (
            f"<code>{result['bic']}</code>"
            if result["bic"]
            else "غير متوفر"
        )
    )

    lines.append(
        "• المدينة: "
        + (
            result["city"]
            or "غير متوفر"
        )
    )

    lines.append(
        "• العنوان: "
        + (
            result["address"]
            or "غير متوفر"
        )
    )

    lines.append("")
    lines.append(
        "💶 <b>SEPA</b>"
    )

    lines.append(
        "• SEPA Normal (SCT): "
        + yes_no_unknown(
            result["sct"]
        )
    )

    lines.append(
        "• SEPA Instant (SCT Inst): "
        + yes_no_unknown(
            result["instant"]
        )
    )

    return "\n".join(
        lines
    )


# ============================================================
# TELEGRAM HANDLER
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    text = (
        update.message.text
        or ""
    ).strip()

    ibans = extract_ibans(
        text
    )

    if not ibans:

        await update.message.reply_text(
            "❌ لم أجد أي IBAN.\n\n"
            "أرسل IBAN واحد أو قائمة IBANs."
        )

        return

    if len(ibans) > MAX_IBANS:

        await update.message.reply_text(
            f"❌ الحد الأقصى "
            f"{MAX_IBANS} IBAN."
        )

        return

    wait = (
        await update.message.reply_text(
            f"⏳ جاري فحص "
            f"{len(ibans)} IBAN..."
        )
    )

    connector = aiohttp.TCPConnector(
        limit=CONCURRENCY
    )

    semaphore = asyncio.Semaphore(
        CONCURRENCY
    )

    async with aiohttp.ClientSession(
        connector=connector,
        headers={
            "User-Agent":
                "European-IBAN-Bot/1.0"
        },
    ) as session:

        # تحديث EPC
        await epc.refresh(
            session
        )

        tasks = [
            process_iban(
                session,
                semaphore,
                iban,
            )
            for iban in ibans
        ]

        results = await asyncio.gather(
            *tasks
        )

    header = (
        "📋 <b>نتائج فحص IBAN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    blocks = []

    for result in results:

        blocks.append(
            format_result(
                result
            )
        )

    body = (
        "\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    ).join(
        blocks
    )

    footer = (
        "\n\n━━━━━━━━━━━━━━━━━━━━\n"
        "🆓 بدون API مدفوع\n"
        "ℹ️ التحقق فني فقط؛ لا يثبت أن "
        "الحساب مفتوح أو فيه رصيد.\n"
        "ℹ️ SEPA مأخوذ من سجلات EPC الرسمية."
    )

    final = (
        header
        + body
        + footer
    )

    # Telegram max message safety
    if len(final) <= 4000:

        await wait.edit_text(
            final,
            parse_mode=ParseMode.HTML,
        )

    else:

        await wait.delete()

        current = header

        for block in blocks:

            piece = (
                block
                + "\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
            )

            if (
                len(current)
                + len(piece)
                > 3800
            ):

                await update.message.reply_text(
                    current,
                    parse_mode=ParseMode.HTML,
                )

                current = piece

            else:

                current += piece

        current += footer

        await update.message.reply_text(
            current[:4090],
            parse_mode=ParseMode.HTML,
        )


# ============================================================
# MAIN
# ============================================================

def main():

    if not TOKEN:

        raise RuntimeError(
            "❌ TELEGRAM_BOT_TOKEN غير موجود."
        )

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message,
        )
    )

    logger.info(
        "European IBAN Bot started."
    )

    application.run_polling()


if __name__ == "__main__":
    main()
