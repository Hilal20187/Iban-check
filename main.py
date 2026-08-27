import os
import logging
import asyncio

import aiohttp
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# دعم الاسمين، مع تصحيح الخطأ الإملائي TELEGARM
TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("TELEGARM_BOT_TOKEN")
)


async def delete_webhook_safely(token: str) -> None:
    """حذف الـ webhook قبل تشغيل polling."""
    url = f"https://api.telegram.org/bot{token}/deleteWebhook"

    try:
        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                json={"drop_pending_updates": True},
            ) as response:

                if response.status != 200:
                    logger.warning(
                        "Failed to delete webhook. HTTP %s",
                        response.status,
                    )
                else:
                    logger.info("Webhook deleted successfully.")

    except aiohttp.ClientError as exc:
        logger.warning("Webhook deletion failed: %s", exc)
    except asyncio.TimeoutError:
        logger.warning("Webhook deletion timed out.")


def extract_ibans(text: str) -> list[str]:
    """استخراج IBANs من النص."""
    ibans = []

    # تقسيم النص إلى كلمات
    for part in text.split():
        # إزالة المسافات والرموز الشائعة
        clean = (
            part.strip()
            .replace(" ", "")
            .replace("-", "")
            .replace("_", "")
            .upper()
        )

        # IBAN يجب أن يبدأ بحرفين ثم يحتوي على أرقام/حروف
        if (
            len(clean) >= 15
            and len(clean) <= 34
            and clean[:2].isalpha()
            and clean[2:].isalnum()
        ):
            if clean not in ibans:
                ibans.append(clean)

    return ibans


async def validate_iban(
    session: aiohttp.ClientSession,
    iban: str,
) -> dict:
    """التحقق من IBAN باستخدام OpenIBAN."""

    url = f"https://openiban.com/validate/{iban}?getBIC=true"

    try:
        async with session.get(url) as response:

            if response.status != 200:
                return {
                    "status": "server_error",
                    "iban": iban,
                }

            data = await response.json()

            if data.get("valid") is True:
                bank_data = data.get("bankData") or {}

                return {
                    "status": "valid",
                    "iban": iban,
                    "bank_name": bank_data.get("name") or "غير متوفر",
                    "bic": bank_data.get("bic") or "غير متوفر",
                    "city": bank_data.get("city") or "غير متوفر",
                }

            return {
                "status": "invalid",
                "iban": iban,
            }

    except asyncio.TimeoutError:
        return {
            "status": "connection_error",
            "iban": iban,
        }

    except aiohttp.ClientError as exc:
        logger.warning("Connection error for %s: %s", iban, exc)

        return {
            "status": "connection_error",
            "iban": iban,
        }

    except Exception as exc:
        logger.exception("Unexpected error validating %s: %s", iban, exc)

        return {
            "status": "connection_error",
            "iban": iban,
        }


def format_result(result: dict) -> str:
    """تنسيق نتيجة IBAN."""

    iban = result["iban"]

    if result["status"] == "invalid":
        return (
            f"❌ `{iban}`\n"
            f"• الحالة: IBAN غير صالح\n"
            f"-----------------------------------"
        )

    if result["status"] == "server_error":
        return (
            f"⚠️ `{iban}`\n"
            f"• تعذر التحقق من الخادم.\n"
            f"-----------------------------------"
        )

    if result["status"] == "connection_error":
        return (
            f"⚠️ `{iban}`\n"
            f"• حدث خطأ أثناء الاتصال بخدمة التحقق.\n"
            f"-----------------------------------"
        )

    bank_name = result["bank_name"]
    bic = result["bic"]
    city = result["city"]

    return (
        f"✅ `{iban}`\n"
        f"• البنك: {bank_name}\n"
        f"• BIC: `{bic}`\n"
        f"• المدينة: {city}\n"
        f"-----------------------------------"
    )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    # التأكد من وجود رسالة ونص
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    if not text:
        await update.message.reply_text(
            "❌ يرجى إرسال رقم IBAN واحد على الأقل."
        )
        return

    ibans = extract_ibans(text)

    if not ibans:
        await update.message.reply_text(
            "❌ لم أجد IBAN صالحاً في الرسالة.\n\n"
            "أرسل IBAN واحداً أو قائمة من أرقام IBAN."
        )
        return

    wait_msg = await update.message.reply_text(
        f"⏳ جاري التحقق من {len(ibans)} IBAN..."
    )

    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(timeout=timeout) as session:

        # تنفيذ عمليات التحقق بالتوازي
        tasks = [
            validate_iban(session, iban)
            for iban in ibans
        ]

        results = await asyncio.gather(*tasks)

    results_output = [
        format_result(result)
        for result in results
    ]

    final_text = (
        f"📋 *نتائج التحقق ({len(ibans)} IBAN)*\n\n"
        + "\n".join(results_output)
    )

    # Telegram يسمح تقريباً بـ 4096 حرف للرسالة النصية.
    # نترك هامشاً بسيطاً.
    if len(final_text) > 4000:
        final_text = (
            final_text[:3950]
            + "\n\n⚠️ تم اقتصاص النتائج بسبب طول الرسالة."
        )

    try:
        await wait_msg.edit_text(
            final_text,
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.exception("Failed to edit Telegram message: %s", exc)

        # محاولة إرسال النتيجة بدون Markdown
        await update.message.reply_text(
            final_text.replace("`", "").replace("*", "")
        )


async def post_init(application) -> None:
    """تنفيذ حذف webhook قبل بدء polling."""
    if TOKEN:
        await delete_webhook_safely(TOKEN)


def main() -> None:

    if not TOKEN:
        logger.error(
            "TELEGRAM_BOT_TOKEN environment variable is not set."
        )
        return

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    logger.info("Bot started.")

    application.run_polling()


if __name__ == "__main__":
    main() 
