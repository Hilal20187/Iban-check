import os
import re
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
# إعدادات
# ============================================================

TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("TELEGARM_BOT_TOKEN")  # دعم الاسم القديم أيضًا
)

OPENIBAN_URL = "https://openiban.com/validate/{}"

REQUEST_TIMEOUT = 20
MAX_IBANS_PER_MESSAGE = 50
MAX_TELEGRAM_MESSAGE_LENGTH = 4000
MAX_CONCURRENT_REQUESTS = 8

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# تنظيف واستخراج IBANs
# ============================================================

def normalize_iban(value: str) -> str:
    """
    إزالة المسافات والرموز غير الضرورية وتحويل الحروف إلى Uppercase.
    """
    return re.sub(r"[^A-Za-z0-9]", "", value).upper()


def looks_like_iban(value: str) -> bool:
    """
    فحص أولي فقط.
    التحقق الحقيقي يتم من OpenIBAN.
    """
    return bool(
        re.fullmatch(r"[A-Z]{2}[0-9A-Z]{13,32}", value)
    )


def extract_ibans(text: str) -> list[str]:
    """
    استخراج IBANs من رسالة تحتوي على:
    - IBAN واحد
    - عدة IBANs
    - IBANs مفصولة بمسافات أو أسطر
    """

    found = []

    # نبحث عن مقاطع تشبه IBAN حتى لو كانت فيها مسافات.
    candidates = re.findall(
        r"\b[A-Za-z]{2}[0-9A-Za-z\s]{13,40}\b",
        text
    )

    for candidate in candidates:
        iban = normalize_iban(candidate)

        if looks_like_iban(iban) and iban not in found:
            found.append(iban)

    # محاولة إضافية للكلمات المفردة
    for token in text.split():
        iban = normalize_iban(token)

        if looks_like_iban(iban) and iban not in found:
            found.append(iban)

    return found


# ============================================================
# معرفة الدولة من كود IBAN
# ============================================================

COUNTRY_NAMES = {
    "AL": "ألبانيا",
    "AD": "أندورا",
    "AT": "النمسا",
    "AZ": "أذربيجان",
    "BH": "البحرين",
    "BE": "بلجيكا",
    "BA": "البوسنة والهرسك",
    "BR": "البرازيل",
    "BG": "بلغاريا",
    "CR": "كوستاريكا",
    "HR": "كرواتيا",
    "CY": "قبرص",
    "CZ": "التشيك",
    "DK": "الدنمارك",
    "DO": "جمهورية الدومينيكان",
    "EE": "إستونيا",
    "FO": "جزر فارو",
    "FI": "فنلندا",
    "FR": "فرنسا",
    "GE": "جورجيا",
    "DE": "ألمانيا",
    "GI": "جبل طارق",
    "GR": "اليونان",
    "GL": "غرينلاند",
    "GT": "غواتيمالا",
    "HU": "المجر",
    "IS": "آيسلندا",
    "IE": "أيرلندا",
    "IL": "إسرائيل",
    "IT": "إيطاليا",
    "JO": "الأردن",
    "KZ": "كازاخستان",
    "XK": "كوسوفو",
    "KW": "الكويت",
    "LV": "لاتفيا",
    "LB": "لبنان",
    "LI": "ليختنشتاين",
    "LT": "ليتوانيا",
    "LU": "لوكسمبورغ",
    "MT": "مالطا",
    "MR": "موريتانيا",
    "MU": "موريشيوس",
    "MD": "مولدوفا",
    "MC": "موناكو",
    "ME": "الجبل الأسود",
    "NL": "هولندا",
    "MK": "مقدونيا الشمالية",
    "NO": "النرويج",
    "OM": "عُمان",
    "PK": "باكستان",
    "PS": "فلسطين",
    "PL": "بولندا",
    "PT": "البرتغال",
    "QA": "قطر",
    "RO": "رومانيا",
    "SM": "سان مارينو",
    "SA": "السعودية",
    "RS": "صربيا",
    "SK": "سلوفاكيا",
    "SI": "سلوفينيا",
    "ES": "إسبانيا",
    "SE": "السويد",
    "CH": "سويسرا",
    "TN": "تونس",
    "TR": "تركيا",
    "UA": "أوكرانيا",
    "AE": "الإمارات",
    "GB": "المملكة المتحدة",
    "VA": "الفاتيكان",
}


def get_country_name(iban: str) -> str:
    country_code = iban[:2]
    return COUNTRY_NAMES.get(country_code, country_code)


# ============================================================
# فحص IBAN واحد
# ============================================================

async def validate_iban(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    iban: str,
) -> dict:

    url = OPENIBAN_URL.format(iban)

    async with semaphore:
        try:
            async with session.get(
                url,
                params={
                    "getBIC": "true",
                    "validateBankCode": "true",
                },
                timeout=aiohttp.ClientTimeout(
                    total=REQUEST_TIMEOUT
                ),
            ) as response:

                if response.status != 200:
                    return {
                        "iban": iban,
                        "status": "server_error",
                        "http_status": response.status,
                    }

                data = await response.json()

                return {
                    "iban": iban,
                    "status": "ok",
                    "data": data,
                }

        except asyncio.TimeoutError:
            return {
                "iban": iban,
                "status": "timeout",
            }

        except aiohttp.ClientError as exc:
            logger.warning(
                "Connection error for %s: %s",
                iban,
                exc,
            )

            return {
                "iban": iban,
                "status": "connection_error",
            }

        except Exception as exc:
            logger.exception(
                "Unexpected error for %s: %s",
                iban,
            )

            return {
                "iban": iban,
                "status": "error",
            }


# ============================================================
# تنسيق النتيجة
# ============================================================

def format_result(result: dict) -> str:
    iban = result["iban"]

    if result["status"] == "timeout":
        return (
            f"⚠️ <code>{iban}</code>\n"
            f"• الحالة: انتهت مهلة الاتصال بالخدمة"
        )

    if result["status"] == "connection_error":
        return (
            f"⚠️ <code>{iban}</code>\n"
            f"• الحالة: تعذر الاتصال بالخدمة"
        )

    if result["status"] == "server_error":
        return (
            f"⚠️ <code>{iban}</code>\n"
            f"• الحالة: خادم التحقق أعاد الخطأ "
            f"{result.get('http_status', 'غير معروف')}"
        )

    if result["status"] != "ok":
        return (
            f"⚠️ <code>{iban}</code>\n"
            f"• الحالة: حدث خطأ غير متوقع"
        )

    data = result["data"]

    valid = data.get("valid", False)

    country = get_country_name(iban)

    if not valid:
        messages = data.get("messages") or []

        reason = "IBAN غير صالح"

        if messages:
            reason = " | ".join(str(x) for x in messages)

        return (
            f"❌ <code>{iban}</code>\n"
            f"• الحالة: <b>غير صالح</b>\n"
            f"• الدولة: {country}\n"
            f"• التفاصيل: {reason}"
        )

    bank_data = data.get("bankData") or {}

    bank_name = bank_data.get("name") or "غير متوفر"
    bic = bank_data.get("bic") or "غير متوفر"
    city = bank_data.get("city") or "غير متوفر"
    zip_code = bank_data.get("zip") or "غير متوفر"
    bank_code = bank_data.get("bankCode") or "غير متوفر"

    check_results = data.get("checkResults") or {}

    bank_code_status = "غير متاح"

    if "bankCode" in check_results:
        bank_code_status = (
            "صحيح"
            if check_results["bankCode"] is True
            else "غير صحيح"
        )

    return (
        f"✅ <code>{iban}</code>\n"
        f"• الحالة: <b>IBAN صالح من ناحية التحقق الفني</b>\n"
        f"• الدولة: {country} (<code>{iban[:2]}</code>)\n"
        f"• البنك: {bank_name}\n"
        f"• BIC: <code>{bic}</code>\n"
        f"• المدينة: {city}\n"
        f"• الرمز البريدي: {zip_code}\n"
        f"• Bank Code: <code>{bank_code}</code>\n"
        f"• فحص Bank Code: {bank_code_status}\n"
        f"• SEPA Instant: ⚠️ غير محدد من المصدر المجاني"
    )


# ============================================================
# Telegram Handler
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    if not text:
        await update.message.reply_text(
            "❌ أرسل IBAN واحدًا أو عدة أرقام IBAN."
        )
        return

    ibans = extract_ibans(text)

    if not ibans:
        await update.message.reply_text(
            "❌ لم أجد IBAN صالحًا في الرسالة.\n\n"
            "مثال:\n"
            "<code>DE89370400440532013000</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if len(ibans) > MAX_IBANS_PER_MESSAGE:
        await update.message.reply_text(
            f"⚠️ الحد الأقصى هو "
            f"{MAX_IBANS_PER_MESSAGE} IBAN في الرسالة الواحدة."
        )
        return

    wait_message = await update.message.reply_text(
        f"⏳ جاري فحص {len(ibans)} IBAN..."
    )

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    connector = aiohttp.TCPConnector(
        limit=MAX_CONCURRENT_REQUESTS,
        ssl=True,
    )

    async with aiohttp.ClientSession(
        connector=connector,
        headers={
            "User-Agent": "Telegram-IBAN-Checker/1.0"
        },
    ) as session:

        tasks = [
            validate_iban(
                session,
                semaphore,
                iban,
            )
            for iban in ibans
        ]

        results = await asyncio.gather(
            *tasks,
            return_exceptions=False,
        )

    formatted_results = [
        format_result(result)
        for result in results
    ]

    header = (
        f"📋 <b>نتائج فحص IBAN</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔢 العدد: <b>{len(ibans)}</b>\n\n"
    )

    body = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n".join(
        formatted_results
    )

    footer = (
        "\n\n━━━━━━━━━━━━━━━━━━━━\n"
        "ℹ️ <i>التحقق يثبت الصلاحية الفنية للـIBAN "
        "ولا يثبت أن الحساب موجود أو مفتوح فعليًا.</i>"
    )

    final_text = header + body + footer

    # Telegram لديه حد لطول الرسالة.
    if len(final_text) <= MAX_TELEGRAM_MESSAGE_LENGTH:
        await wait_message.edit_text(
            final_text,
            parse_mode=ParseMode.HTML,
        )
        return

    # إذا كانت النتائج كثيرة، نقسمها إلى عدة رسائل.
    chunks = []

    current = header

    for result_text in formatted_results:
        separator = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n"

        candidate = current + result_text + separator

        if len(candidate) > MAX_TELEGRAM_MESSAGE_LENGTH - 300:
            chunks.append(current)
            current = result_text + separator
        else:
            current = candidate

    if current.strip():
        chunks.append(current)

    # أول رسالة بدل رسالة الانتظار
    first_chunk = chunks[0]

    await wait_message.edit_text(
        first_chunk,
        parse_mode=ParseMode.HTML,
    )

    # باقي الرسائل
    for chunk in chunks[1:]:
        await update.message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
        )

    await update.message.reply_text(
        "ℹ️ التحقق الفني لا يثبت وجود الحساب أو نشاطه فعليًا.",
    )


# ============================================================
# حذف Webhook القديم
# ============================================================

async def delete_webhook_safely(token: str):
    url = (
        f"https://api.telegram.org/bot{token}"
        f"/deleteWebhook"
    )

    try:
        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            async with session.get(
                url,
                params={
                    "drop_pending_updates": "true"
                },
            ) as response:

                if response.status == 200:
                    logger.info(
                        "Webhook deleted successfully."
                    )
                else:
                    logger.warning(
                        "Could not delete webhook. HTTP %s",
                        response.status,
                    )

    except Exception as exc:
        logger.warning(
            "Webhook deletion failed: %s",
            exc,
        )


# ============================================================
# تشغيل البوت
# ============================================================

def main():
    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN غير موجود. "
            "ضع توكن البوت في متغير البيئة."
        )

    asyncio.run(
        delete_webhook_safely(TOKEN)
    )

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            handle_message,
        )
    )

    logger.info("IBAN Telegram Bot is starting...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
