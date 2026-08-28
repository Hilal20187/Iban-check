import os
import re
import csv
import io
import asyncio
import logging

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

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

IBANTOOLS_VALIDATE = (
    "https://www.ibantools.org/api/v1/iban/validate/{}"
)

IBANTOOLS_SWIFT = (
    "https://www.ibantools.org/api/v1/swift/{}"
)

IBANTOOLS_SWIFT_SEARCH = (
    "https://www.ibantools.org/api/v1/swift/search"
)

# Official EPC registers
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

# Official Bank of Lithuania financial institution codes
LITHUANIA_CODES_CSV = (
    "https://www.lb.lt/uploads/documents/files/"
    "Finans%C5%B3%20%C4%AFstaig%C5%B3%20kod%C5%B3%20%C5%BEinynas.csv"
)

MAX_IBANS = 30
TIMEOUT = 20
CONCURRENCY = 6
EPC_REFRESH = 6 * 60 * 60

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("iban-bot")


# ============================================================
# COUNTRY NAMES
# ============================================================

COUNTRIES = {
    "AT": "النمسا",
    "BE": "بلجيكا",
    "BG": "بلغاريا",
    "CH": "سويسرا",
    "CY": "قبرص",
    "CZ": "التشيك",
    "DE": "ألمانيا",
    "DK": "الدنمارك",
    "EE": "إستونيا",
    "ES": "إسبانيا",
    "FI": "فنلندا",
    "FR": "فرنسا",
    "GB": "المملكة المتحدة",
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
    "MT": "مالطا",
    "NL": "هولندا",
    "NO": "النرويج",
    "PL": "بولندا",
    "PT": "البرتغال",
    "RO": "رومانيا",
    "SE": "السويد",
    "SI": "سلوفينيا",
    "SK": "سلوفاكيا",
    "SM": "سان مارينو",
    "VA": "الفاتيكان",
}


# ============================================================
# HELPERS
# ============================================================

def normalize(value):
    if value is None:
        return ""

    return re.sub(
        r"[^A-Z0-9]",
        "",
        str(value).upper(),
    )


def normalize_iban(value):
    return normalize(value)


def country_name(code):
    return COUNTRIES.get(
        code,
        code,
    )


def get_field(data, *names):
    """
    يبحث عن الحقل حتى لو اختلفت طريقة كتابته.
    """

    if not isinstance(data, dict):
        return None

    normalized = {}

    for key, value in data.items():
        nk = re.sub(
            r"[^a-z0-9]",
            "",
            str(key).lower(),
        )

        normalized[nk] = value

    for name in names:
        nk = re.sub(
            r"[^a-z0-9]",
            "",
            name.lower(),
        )

        if nk in normalized:
            value = normalized[nk]

            if value not in (
                None,
                "",
                "null",
            ):
                return value

    return None


# ============================================================
# EXTRACT IBANS
# ============================================================

def extract_ibans(text):
    found = []

    # البحث عن IBAN مع احتمال وجود مسافات
    candidates = re.findall(
        r"\b[A-Za-z]{2}[0-9A-Za-z\s]{13,40}\b",
        text,
    )

    for candidate in candidates:
        iban = normalize_iban(candidate)

        if (
            re.fullmatch(
                r"[A-Z]{2}[0-9A-Z]{13,32}",
                iban,
            )
            and iban not in found
        ):
            found.append(iban)

    # fallback
    for token in text.split():
        iban = normalize_iban(token)

        if (
            re.fullmatch(
                r"[A-Z]{2}[0-9A-Z]{13,32}",
                iban,
            )
            and iban not in found
        ):
            found.append(iban)

    return found


# ============================================================
# EPC DATABASE
# ============================================================

class EPCDatabase:

    def __init__(self):
        self.sct = {}
        self.instant = {}
        self.updated = 0
        self.lock = asyncio.Lock()

    async def download(self, session, url):
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(
                    total=60
                ),
                headers={
                    "User-Agent": "Free-IBAN-Bot/1.0"
                },
            ) as response:

                if response.status != 200:
                    logger.warning(
                        "EPC download HTTP %s",
                        response.status,
                    )
                    return []

                raw = await response.read()

                text = raw.decode(
                    "utf-8-sig",
                    errors="replace",
                )

                # EPC CSV normally uses ;
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
                "EPC download error: %s",
                exc,
            )
            return []

    def index(self, rows):
        database = {}

        for row in rows:
            clean = {}

            for key, value in row.items():
                clean[
                    re.sub(
                        r"[^A-Z0-9]",
                        "",
                        str(key).upper(),
                    )
                ] = (
                    str(value).strip()
                    if value is not None
                    else ""
                )

            bic = (
                clean.get("BIC")
                or clean.get("BIC11")
                or clean.get("BIC8")
                or clean.get("BICCODE")
            )

            if not bic:
                continue

            bic = normalize(bic)

            database[bic] = clean

        return database

    async def refresh(self, session):
        now = asyncio.get_running_loop().time()

        if (
            self.updated
            and now - self.updated < EPC_REFRESH
        ):
            return

        async with self.lock:

            now = asyncio.get_running_loop().time()

            if (
                self.updated
                and now - self.updated < EPC_REFRESH
            ):
                return

            logger.info(
                "Updating official EPC registers..."
            )

            sct_rows, instant_rows = await asyncio.gather(
                self.download(
                    session,
                    EPC_SCT_CSV,
                ),
                self.download(
                    session,
                    EPC_SCT_INST_CSV,
                ),
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
                "EPC: SCT=%s SCT_INST=%s",
                len(self.sct),
                len(self.instant),
            )

    def check_bic(self, bic):
        if not bic:
            return {
                "sct": None,
                "instant": None,
                "sct_data": {},
                "instant_data": {},
            }

        bic = normalize(bic)
        bic8 = bic[:8]

        sct_data = (
            self.sct.get(bic)
            or self.sct.get(bic8)
            or {}
        )

        instant_data = (
            self.instant.get(bic)
            or self.instant.get(bic8)
            or {}
        )

        return {
            "sct": bool(sct_data),
            "instant": bool(instant_data),
            "sct_data": sct_data,
            "instant_data": instant_data,
        }


epc = EPCDatabase()


# ============================================================
# LITHUANIA BANK CODE DATABASE
# ============================================================

class LithuaniaDatabase:

    def __init__(self):
        self.data = {}
        self.updated = 0

    async def refresh(self, session):
        """
        محاولة تحميل سجل بنك ليتوانيا الرسمي.

        إذا تغير رابط الملف مستقبلاً، يبقى IBANTools
        كـ fallback.
        """

        try:
            async with session.get(
                LITHUANIA_CODES_CSV,
                timeout=aiohttp.ClientTimeout(
                    total=60
                ),
            ) as response:

                if response.status != 200:
                    logger.warning(
                        "Lithuania CSV HTTP %s",
                        response.status,
                    )
                    return

                raw = await response.read()

                text = raw.decode(
                    "utf-8-sig",
                    errors="replace",
                )

                # Lithuanian official file can change delimiter.
                try:
                    dialect = csv.Sniffer().sniff(
                        text[:10000],
                        delimiters=";,\t"
                    )
                except Exception:
                    dialect = csv.excel

                rows = csv.DictReader(
                    io.StringIO(text),
                    dialect=dialect,
                )

                self.data = {}

                for row in rows:

                    clean = {}

                    for key, value in row.items():
                        key_clean = re.sub(
                            r"[^A-Z0-9]",
                            "",
                            str(key).upper(),
                        )

                        clean[key_clean] = (
                            str(value).strip()
                            if value is not None
                            else ""
                        )

                    # Find a 5-digit financial institution code
                    possible = []

                    for key, value in clean.items():
                        if (
                            value.isdigit()
                            and len(value) == 5
                        ):
                            possible.append(value)

                    for code in possible:
                        self.data[code] = clean

                self.updated = (
                    asyncio.get_running_loop().time()
                )

                logger.info(
                    "Lithuania financial codes loaded: %s",
                    len(self.data),
                )

        except Exception as exc:
            logger.warning(
                "Lithuania database error: %s",
                exc,
            )

    def lookup(self, code):
        return self.data.get(
            normalize(code),
            {},
        )


lt_db = LithuaniaDatabase()


# ============================================================
# IBANTools VALIDATION
# ============================================================

async def validate_iban(
    session,
    semaphore,
    iban,
):

    async with semaphore:

        try:
            url = IBANTOOLS_VALIDATE.format(
                iban
            )

            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(
                    total=TIMEOUT
                ),
            ) as response:

                if response.status != 200:
                    return {
                        "iban": iban,
                        "error": (
                            f"IBANTools HTTP "
                            f"{response.status}"
                        ),
                    }

                data = await response.json()

                return {
                    "iban": iban,
                    "data": data,
                }

        except asyncio.TimeoutError:
            return {
                "iban": iban,
                "error": "انتهت مهلة الاتصال",
            }

        except Exception as exc:
            logger.warning(
                "Validation error: %s",
                exc,
            )

            return {
                "iban": iban,
                "error": "تعذر الاتصال بخدمة IBANTools",
            }


# ============================================================
# SWIFT LOOKUP
# ============================================================

async def swift_lookup(
    session,
    semaphore,
    bic,
):

    if not bic:
        return {}

    async with semaphore:

        try:
            url = IBANTOOLS_SWIFT.format(
                normalize(bic)
            )

            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(
                    total=TIMEOUT
                ),
            ) as response:

                if response.status != 200:
                    return {}

                data = await response.json()

                if isinstance(data, dict):
                    return data

                return {}

        except Exception:
            return {}


# ============================================================
# GET BANK INFORMATION
# ============================================================

async def get_bank_info(
    session,
    semaphore,
    iban,
    data,
):

    country = iban[:2]

    bank_code = get_field(
        data,
        "bank_code",
        "bankcode",
    )

    bic = get_field(
        data,
        "bic",
        "swift",
        "swift_code",
        "bic_code",
    )

    bank_name = get_field(
        data,
        "bank_name",
        "bank",
        "institution",
        "name",
    )

    city = get_field(
        data,
        "city",
        "bank_city",
    )

    address = get_field(
        data,
        "address",
        "bank_address",
    )

    postal = get_field(
        data,
        "postal_code",
        "zip",
        "postcode",
    )

    # --------------------------------------------------------
    # Lithuania official database
    # --------------------------------------------------------

    if country == "LT" and bank_code:

        lt = lt_db.lookup(
            str(bank_code)
        )

        if lt:

            # Try common field names
            bank_name = (
                bank_name
                or lt.get("PAVADINIMAS")
                or lt.get("PAVADINIMASEN")
                or lt.get("NAME")
                or lt.get("BANKNAME")
            )

            bic = (
                bic
                or lt.get("BIC")
                or lt.get("SWIFT")
            )

            city = (
                city
                or lt.get("MIESTAS")
                or lt.get("CITY")
            )

            address = (
                address
                or lt.get("ADRESAS")
                or lt.get("ADDRESS")
            )

            postal = (
                postal
                or lt.get("PASTOADRESAS")
                or lt.get("POSTALCODE")
                or lt.get("ZIP")
            )

    # --------------------------------------------------------
    # If BIC exists, get detailed BIC information
    # --------------------------------------------------------

    if bic:

        swift_data = await swift_lookup(
            session,
            semaphore,
            str(bic),
        )

        bank_name = (
            bank_name
            or get_field(
                swift_data,
                "bank_name",
                "name",
                "institution",
            )
        )

        city = (
            city
            or get_field(
                swift_data,
                "city",
                "bank_city",
            )
        )

        address = (
            address
            or get_field(
                swift_data,
                "address",
                "bank_address",
            )
        )

        postal = (
            postal
            or get_field(
                swift_data,
                "postal_code",
                "zip",
                "postcode",
            )
        )

    return {
        "bank_code": bank_code,
        "bic": bic,
        "bank_name": bank_name,
        "city": city,
        "address": address,
        "postal": postal,
    }


# ============================================================
# FORMAT RESULT
# ============================================================

def format_result(
    result,
    bank,
    sepa,
):

    iban = result["iban"]

    if "error" in result:
        return (
            f"⚠️ <code>{iban}</code>\n"
            f"• الحالة: {result['error']}"
        )

    data = result.get("data") or {}

    valid = get_field(
        data,
        "valid",
        "is_valid",
    )

    country = iban[:2]

    if valid is False:
        return (
            f"❌ <code>{iban}</code>\n"
            f"• الحالة: <b>IBAN غير صالح</b>\n"
            f"• الدولة: {country_name(country)}"
        )

    bank_code = bank.get(
        "bank_code"
    ) or "غير متوفر"

    bic = bank.get(
        "bic"
    ) or "غير متوفر"

    bank_name = bank.get(
        "bank_name"
    ) or "غير متوفر"

    city = bank.get(
        "city"
    ) or "غير متوفر"

    address = bank.get(
        "address"
    ) or "غير متوفر"

    postal = bank.get(
        "postal"
    ) or "غير متوفر"

    sct = sepa["sct"]

    instant = sepa["instant"]

    sct_text = (
        "✅ نعم"
        if sct is True
        else "❌ لا"
        if sct is False
        else "⚠️ غير محدد"
    )

    instant_text = (
        "⚡ نعم"
        if instant is True
        else "❌ لا"
        if instant is False
        else "⚠️ غير محدد"
    )

    return (
        f"✅ <code>{iban}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• الحالة: <b>IBAN صالح</b>\n"
        f"• الدولة: {country_name(country)} "
        f"(<code>{country}</code>)\n"
        f"• Bank Code: <code>{bank_code}</code>\n\n"

        f"🏦 <b>البنك</b>\n"
        f"• الاسم: {bank_name}\n"
        f"• BIC/SWIFT: <code>{bic}</code>\n"
        f"• المدينة: {city}\n"
        f"• العنوان: {address}\n"
        f"• الرمز البريدي: {postal}\n\n"

        f"💶 <b>SEPA Normal (SCT):</b> "
        f"{sct_text}\n"
        f"⚡ <b>SEPA Instant (SCT Inst):</b> "
        f"{instant_text}\n\n"

        f"ℹ️ <i>SEPA يعتمد على سجل EPC "
        f"الرسمي، وليس على تخمين اسم البنك.</i>"
    )


# ============================================================
# TELEGRAM
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

    ibans = extract_ibans(text)

    if not ibans:
        await update.message.reply_text(
            "❌ أرسل IBAN واحدًا أو قائمة IBANs."
        )
        return

    if len(ibans) > MAX_IBANS:
        await update.message.reply_text(
            f"❌ الحد الأقصى {MAX_IBANS} IBAN."
        )
        return

    wait = await update.message.reply_text(
        f"⏳ جاري التحقق من {len(ibans)} IBAN..."
    )

    semaphore = asyncio.Semaphore(
        CONCURRENCY
    )

    connector = aiohttp.TCPConnector(
        limit=CONCURRENCY
    )

    async with aiohttp.ClientSession(
        connector=connector,
        headers={
            "User-Agent":
                "Free-IBAN-Telegram-Bot/2.0"
        },
    ) as session:

        # تحديث قواعد EPC
        await epc.refresh(session)

        # تحديث قاعدة ليتوانيا
        await lt_db.refresh(session)

        # تحقق IBAN
        tasks = [
            validate_iban(
                session,
                semaphore,
                iban,
            )
            for iban in ibans
        ]

        results = await asyncio.gather(
            *tasks
        )

        formatted = []

        for result in results:

            if "error" in result:

                formatted.append(
                    format_result(
                        result,
                        {},
                        {
                            "sct": None,
                            "instant": None,
                        },
                    )
                )

                continue

            data = result.get(
                "data"
            ) or {}

            bank = await get_bank_info(
                session,
                semaphore,
                result["iban"],
                data,
            )

            bic = bank.get(
                "bic"
            )

            sepa = epc.check_bic(
                bic
            )

            formatted.append(
                format_result(
                    result,
                    bank,
                    {
                        "sct": sepa["sct"],
                        "instant": sepa["instant"],
                    },
                )
            )

    header = (
        "📋 <b>نتائج فحص IBAN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    body = (
        "\n\n━━━━━━━━━━━━━━━━━━━━\n\n"
        .join(formatted)
    )

    footer = (
        "\n\n━━━━━━━━━━━━━━━━━━━━\n"
        "🆓 مجاني — لا يحتاج API Key\n"
        "⚠️ صلاحية IBAN لا تعني أن الحساب "
        "مفتوح أو يحتوي على رصيد.\n"
        "⚠️ BIC في EPC هو Reference BIC."
    )

    final = (
        header
        + body
        + footer
    )

    if len(final) <= 4000:

        await wait.edit_text(
            final,
            parse_mode=ParseMode.HTML,
        )

    else:

        await wait.delete()

        chunks = []
        current = header

        for item in formatted:

            part = (
                item
                + "\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
            )

            if len(current) + len(part) > 3700:
                chunks.append(current)
                current = part
            else:
                current += part

        if current:
            chunks.append(current)

        for i, chunk in enumerate(chunks):

            if i == len(chunks) - 1:
                chunk += footer

            await update.message.reply_text(
                chunk,
                parse_mode=ParseMode.HTML,
            )


# ============================================================
# MAIN
# ============================================================

def main():

    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN غير موجود."
        )

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & (~filters.COMMAND),
            handle_message,
        )
    )

    logger.info(
        "Free IBAN checker started."
    )

    application.run_polling()


if __name__ == "__main__":
    main()
