import os
import re
import csv
import io
import asyncio
import logging
from typing import Optional

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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

IBANTOOLS_VALIDATE_URL = (
    "https://www.ibantools.org/api/v1/iban/validate/{}"
)

IBANTOOLS_SWIFT_SEARCH_URL = (
    "https://www.ibantools.org/api/v1/swift/search"
)

# Official EPC registers
EPC_SCT_CSV = (
    "https://www.europeanpaymentscouncil.eu/"
    "sites/default/files/participants_export/sct/sct.csv"
)

EPC_SCT_INST_CSV = (
    "https://www.europeanpaymentscouncil.eu/"
    "sites/default/files/participants_export/"
    "sct_inst/sct_inst.csv"
)

MAX_IBANS = 50
REQUEST_TIMEOUT = 20
CONCURRENCY = 8

# Refresh EPC registers every 6 hours
EPC_REFRESH_SECONDS = 6 * 60 * 60

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# EPC CACHE
# ============================================================

class EPCRegister:
    def __init__(self):
        self.sct = {}
        self.sct_inst = {}
        self.last_update = 0
        self.lock = asyncio.Lock()

    @staticmethod
    def normalize(value):
        if value is None:
            return ""

        return re.sub(
            r"[^A-Z0-9]",
            "",
            str(value).upper(),
        )

    async def download_csv(self, session, url):
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(
                    total=60
                ),
                headers={
                    "User-Agent": "Free-IBAN-Telegram-Bot/1.0"
                },
            ) as response:

                if response.status != 200:
                    logger.warning(
                        "EPC HTTP %s: %s",
                        response.status,
                        url,
                    )
                    return []

                content = await response.read()

                # EPC CSV may contain BOM
                text = content.decode(
                    "utf-8-sig",
                    errors="replace",
                )

                # Try semicolon first, then comma.
                try:
                    dialect = csv.Sniffer().sniff(
                        text[:10000],
                        delimiters=",;"
                    )
                except csv.Error:
                    dialect = csv.excel

                reader = csv.DictReader(
                    io.StringIO(text),
                    dialect=dialect,
                )

                return list(reader)

        except Exception as exc:
            logger.warning(
                "EPC download failed: %s",
                exc,
            )
            return []

    def index_rows(self, rows):
        """
        Index by BIC/reference BIC.

        EPC explicitly warns that these BICs are reference BICs,
        not necessarily routing BICs.
        """

        result = {}

        for row in rows:
            normalized = {
                self.normalize(k): (
                    str(v).strip()
                    if v is not None
                    else ""
                )
                for k, v in row.items()
            }

            # EPC files normally contain BIC.
            possible_bics = [
                normalized.get("BIC", ""),
                normalized.get("BIC11", ""),
                normalized.get("BIC8", ""),
                normalized.get("BICCODE", ""),
            ]

            bic = ""

            for candidate in possible_bics:
                if candidate:
                    bic = candidate
                    break

            if bic:
                result[bic] = normalized

        return result

    async def refresh(self, session, force=False):
        async with self.lock:
            now = asyncio.get_running_loop().time()

            if (
                not force
                and self.last_update
                and now - self.last_update
                < EPC_REFRESH_SECONDS
            ):
                return

            logger.info(
                "Downloading official EPC registers..."
            )

            sct_rows, instant_rows = await asyncio.gather(
                self.download_csv(
                    session,
                    EPC_SCT_CSV,
                ),
                self.download_csv(
                    session,
                    EPC_SCT_INST_CSV,
                ),
            )

            if sct_rows:
                self.sct = self.index_rows(
                    sct_rows
                )

            if instant_rows:
                self.sct_inst = self.index_rows(
                    instant_rows
                )

            self.last_update = now

            logger.info(
                "EPC loaded: SCT=%d, SCT_INST=%d",
                len(self.sct),
                len(self.sct_inst),
            )

    def check(self, bic: str):
        bic = self.normalize(bic)

        if not bic:
            return {
                "sct": None,
                "sct_inst": None,
            }

        # BIC can be 8 or 11 chars.
        bic8 = bic[:8]

        sct_match = (
            bic in self.sct
            or bic8 in self.sct
        )

        instant_match = (
            bic in self.sct_inst
            or bic8 in self.sct_inst
        )

        return {
            "sct": sct_match,
            "sct_inst": instant_match,
        }


epc = EPCRegister()


# ============================================================
# IBAN EXTRACTION
# ============================================================

def normalize_iban(value: str) -> str:
    return re.sub(
        r"[^A-Za-z0-9]",
        "",
        value,
    ).upper()


def is_possible_iban(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"[A-Z]{2}[0-9A-Z]{13,32}",
            value,
        )
    )


def extract_ibans(text: str):
    found = []

    # Remove spaces from possible IBAN groups.
    candidates = re.findall(
        r"\b[A-Za-z]{2}[0-9A-Za-z\s]{13,40}\b",
        text,
    )

    for candidate in candidates:
        iban = normalize_iban(candidate)

        if (
            is_possible_iban(iban)
            and iban not in found
        ):
            found.append(iban)

    # Fallback token-by-token
    for token in text.split():
        iban = normalize_iban(token)

        if (
            is_possible_iban(iban)
            and iban not in found
        ):
            found.append(iban)

    return found


# ============================================================
# IBANTOOLS
# ============================================================

async def validate_iban(
    session,
    semaphore,
    iban,
):
    async with semaphore:
        try:
            url = IBANTOOLS_VALIDATE_URL.format(
                iban
            )

            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(
                    total=REQUEST_TIMEOUT
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
                "IBAN validation error: %s",
                exc,
            )

            return {
                "iban": iban,
                "error": "تعذر الاتصال بخدمة التحقق",
            }


# ============================================================
# SWIFT / BIC SEARCH
# ============================================================

async def search_bic(
    session,
    semaphore,
    query,
    country,
):
    """
    IBANTools يسمح بالبحث عن SWIFT/BIC
    حسب البنك أو المدينة أو الكود.

    هذا البحث ليس تخمينًا، لكنه قد يرجع عدة نتائج.
    """

    if not query:
        return []

    async with semaphore:
        try:
            async with session.get(
                IBANTOOLS_SWIFT_SEARCH_URL,
                params={
                    "q": query,
                    "country": country,
                },
                timeout=aiohttp.ClientTimeout(
                    total=REQUEST_TIMEOUT
                ),
            ) as response:

                if response.status != 200:
                    return []

                data = await response.json()

                if isinstance(data, list):
                    return data

                if isinstance(data, dict):
                    for key in (
                        "results",
                        "data",
                        "swifts",
                    ):
                        if isinstance(
                            data.get(key),
                            list,
                        ):
                            return data[key]

                return []

        except Exception:
            return []


# ============================================================
# COUNTRY
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
    "GB": "المملكة المتحدة",
}


def country_name(code):
    return COUNTRIES.get(
        code,
        code,
    )


# ============================================================
# GET FIELD
# ============================================================

def get_first(data, names):
    """
    لأن أسماء الحقول قد تختلف حسب استجابة API.
    """

    if not isinstance(data, dict):
        return None

    normalized = {
        re.sub(
            r"[^a-z0-9]",
            "",
            str(k).lower(),
        ): v
        for k, v in data.items()
    }

    for name in names:
        key = re.sub(
            r"[^a-z0-9]",
            "",
            name.lower(),
        )

        value = normalized.get(key)

        if value not in (
            None,
            "",
            "null",
        ):
            return value

    return None


# ============================================================
# FIND BIC
# ============================================================

async def get_bic(
    session,
    semaphore,
    iban_data,
    country,
):
    """
    نحاول أولًا أخذ BIC من استجابة IBANTools
    إن توفر.

    إذا لم يتوفر، نبحث باستخدام bank code.
    """

    bic = get_first(
        iban_data,
        [
            "bic",
            "swift",
            "swift_code",
            "bic_code",
        ],
    )

    if bic:
        return {
            "bic": str(bic).upper(),
            "bank": get_first(
                iban_data,
                [
                    "bank_name",
                    "bank",
                    "name",
                ],
            ),
            "address": get_first(
                iban_data,
                [
                    "address",
                    "bank_address",
                ],
            ),
            "city": get_first(
                iban_data,
                [
                    "city",
                    "bank_city",
                ],
            ),
        }

    bank_code = get_first(
        iban_data,
        [
            "bank_code",
            "bankcode",
        ],
    )

    if not bank_code:
        return {}

    results = await search_bic(
        session,
        semaphore,
        str(bank_code),
        country,
    )

    if not results:
        return {}

    # نختار أول نتيجة مطابقة للدولة.
    item = results[0]

    if not isinstance(item, dict):
        return {}

    return {
        "bic": get_first(
            item,
            [
                "bic",
                "swift",
                "swift_code",
            ],
        ),
        "bank": get_first(
            item,
            [
                "bank_name",
                "name",
                "institution",
            ],
        ),
        "address": get_first(
            item,
            [
                "address",
                "bank_address",
            ],
        ),
        "city": get_first(
            item,
            [
                "city",
                "bank_city",
            ],
        ),
    }


# ============================================================
# FORMAT
# ============================================================

def format_result(
    result,
    bank_info,
    sepa_info,
):
    iban = result["iban"]

    if "error" in result:
        return (
            f"⚠️ <code>{iban}</code>\n"
            f"• الحالة: {result['error']}"
        )

    data = result.get("data") or {}

    valid = get_first(
        data,
        [
            "valid",
            "is_valid",
        ],
    )

    country = iban[:2]

    if valid is False:
        return (
            f"❌ <code>{iban}</code>\n"
            f"• الحالة: <b>IBAN غير صالح</b>\n"
            f"• الدولة: {country_name(country)}"
        )

    bank_code = get_first(
        data,
        [
            "bank_code",
            "bankcode",
        ],
    )

    branch_code = get_first(
        data,
        [
            "branch_code",
            "branchcode",
        ],
    )

    bic = bank_info.get("bic")
    bank = bank_info.get("bank")
    address = bank_info.get("address")
    city = bank_info.get("city")

    bank = bank or "غير متوفر"
    bic = bic or "غير متوفر"
    address = address or "غير متوفر"
    city = city or "غير متوفر"
    bank_code = bank_code or "غير متوفر"
    branch_code = branch_code or "غير متوفر"

    sct = sepa_info.get("sct")
    instant = sepa_info.get("sct_inst")

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
        f"• الحالة: <b>IBAN صالح</b>\n"
        f"• الدولة: {country_name(country)} "
        f"(<code>{country}</code>)\n"
        f"• البنك: {bank}\n"
        f"• BIC/SWIFT: <code>{bic}</code>\n"
        f"• Bank Code: <code>{bank_code}</code>\n"
        f"• Branch Code: <code>{branch_code}</code>\n"
        f"• المدينة: {city}\n"
        f"• العنوان: {address}\n\n"
        f"💶 <b>SEPA Normal (SCT):</b> "
        f"{sct_text}\n"
        f"⚡ <b>SEPA Instant (SCT Inst):</b> "
        f"{instant_text}\n\n"
        f"ℹ️ RT1/TIPS: لا يتم تخمينهما من هذه البيانات."
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

    text = update.message.text or ""

    ibans = extract_ibans(text)

    if not ibans:
        await update.message.reply_text(
            "❌ لم أجد IBAN في الرسالة.\n\n"
            "مثال:\n"
            "<code>LT303250098266887526</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if len(ibans) > MAX_IBANS:
        await update.message.reply_text(
            f"❌ الحد الأقصى هو {MAX_IBANS} IBAN."
        )
        return

    wait = await update.message.reply_text(
        f"⏳ جاري التحقق من {len(ibans)} IBAN..."
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
                "Free-IBAN-Telegram-Bot/1.0"
        },
    ) as session:

        # تحديث EPC
        await epc.refresh(session)

        # تحقق IBANs
        validation_tasks = [
            validate_iban(
                session,
                semaphore,
                iban,
            )
            for iban in ibans
        ]

        results = await asyncio.gather(
            *validation_tasks
        )

        output = []

        for result in results:

            if "error" in result:
                output.append(
                    format_result(
                        result,
                        {},
                        {
                            "sct": None,
                            "sct_inst": None,
                        },
                    )
                )
                continue

            data = result.get("data") or {}

            country = result["iban"][:2]

            # الحصول على BIC/بيانات البنك
            bank_info = await get_bic(
                session,
                semaphore,
                data,
                country,
            )

            bic = bank_info.get("bic")

            # SEPA من EPC
            sepa_info = epc.check(
                bic or ""
            )

            output.append(
                format_result(
                    result,
                    bank_info,
                    sepa_info,
                )
            )

    header = (
        "📋 <b>نتائج فحص IBAN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    body = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n".join(
        output
    )

    footer = (
        "\n\n━━━━━━━━━━━━━━━━━━━━\n"
        "🆓 <b>المصادر:</b> IBANTools + EPC\n"
        "ℹ️ SEPA/SCT وSCT Inst مبنيان على "
        "سجلات EPC الرسمية.\n"
        "⚠️ صلاحية IBAN لا تعني أن الحساب مفتوح "
        "أو يحتوي على أموال."
    )

    final = header + body + footer

    # Telegram limit
    if len(final) <= 4000:
        await wait.edit_text(
            final,
            parse_mode=ParseMode.HTML,
        )
        return

    # إرسال مقسم
    await wait.delete()

    chunks = []
    current = header

    for item in output:
        part = item + "\n\n━━━━━━━━━━━━━━━━━━━━\n\n"

        if len(current) + len(part) > 3800:
            chunks.append(current)
            current = part
        else:
            current += part

    if current:
        chunks.append(current)

    for index, chunk in enumerate(chunks):
        if index == len(chunks) - 1:
            chunk += footer

        await update.message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
        )


# ============================================================
# MAIN
# ============================================================

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "ضع TELEGRAM_BOT_TOKEN في متغير البيئة."
        )

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            handle_message,
        )
    )

    logger.info(
        "Free IBAN Telegram Bot started."
    )

    application.run_polling()


if __name__ == "__main__":
    main()
