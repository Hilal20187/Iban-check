import os
import re
import asyncio
import logging

import aiohttp
from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================================================
# SETTINGS
# =========================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

BASE_URL = "https://www.ibancalculator.com/validate/"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("iban-bot")


# =========================================================
# IBAN
# =========================================================

def clean_iban(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def extract_ibans(text: str):
    results = []

    # يسمح بـ IBAN فيه مسافات
    matches = re.findall(
        r"\b[A-Z]{2}\s*[0-9]{2}(?:[\sA-Z0-9]{10,40})\b",
        text.upper(),
    )

    for item in matches:
        iban = clean_iban(item)

        if (
            len(iban) >= 15
            and re.match(r"^[A-Z]{2}[0-9]{2}", iban)
            and iban not in results
        ):
            results.append(iban)

    # fallback
    for word in text.split():
        iban = clean_iban(word)

        if (
            len(iban) >= 15
            and re.match(r"^[A-Z]{2}[0-9]{2}", iban)
            and iban not in results
        ):
            results.append(iban)

    return results


# =========================================================
# TEXT HELPERS
# =========================================================

def normalize_spaces(text):
    return re.sub(r"[ \t]+", " ", text).strip()


def clean_lines(text):
    lines = []

    for line in text.splitlines():
        line = normalize_spaces(line)

        if line:
            lines.append(line)

    return lines


def find_line_value(lines, labels):
    """
    يبحث عن:
    Bank: ABC
    أو
    Bank
    ABC
    """

    labels_lower = [
        x.lower()
        for x in labels
    ]

    for i, line in enumerate(lines):

        low = line.lower().strip()

        # نفس السطر
        for label in labels_lower:

            if low.startswith(label):

                rest = line[
                    len(label):
                ].strip(" :|-")

                if rest:
                    return rest

        # السطر التالي
        if low in labels_lower:

            if i + 1 < len(lines):
                return lines[i + 1].strip()

    return None


def find_boolean(lines, phrases):
    text = "\n".join(lines).lower()

    for phrase in phrases:

        if phrase.lower() in text:

            # نحدد هل supported أم not supported
            index = text.find(
                phrase.lower()
            )

            nearby = text[
                max(0, index - 100):
                index + 200
            ]

            if "not supported" in nearby:
                return False

            if "nicht unterstützt" in nearby:
                return False

            if "supported" in nearby:
                return True

            if "unterstützt" in nearby:
                return True

    return None


# =========================================================
# PARSE IBAN CALCULATOR
# =========================================================

def parse_page(html, iban):

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    # نحذف السكربتات
    for tag in soup(
        ["script", "style", "noscript"]
    ):
        tag.decompose()

    text = soup.get_text(
        "\n",
        strip=True,
    )

    lines = clean_lines(text)

    result = {
        "iban": iban,
        "valid": None,
        "bic": None,
        "bank": None,
        "address": None,
        "branch": None,
        "sepa": None,
        "direct_debit": None,
        "b2b": None,
        "instant": None,
        "data_date": None,
    }

    lower = text.lower()

    # =====================================================
    # VALID
    # =====================================================

    if (
        "this is a valid iban" in lower
        or "this iban is valid" in lower
        or "dies ist eine gültige iban" in lower
        or "dies ist eine gültige iban." in lower
    ):
        result["valid"] = True

    elif (
        "this iban is incorrect" in lower
        or "this is an invalid iban" in lower
        or "this iban is invalid" in lower
        or "dies ist keine gültige iban" in lower
    ):
        result["valid"] = False

    # =====================================================
    # BIC
    # =====================================================

    for i, line in enumerate(lines):

        if line.lower().startswith("bic:"):

            value = line.split(
                ":",
                1
            )[1].strip()

            # أحيانًا يكون بعد BIC اسم المدينة بين قوسين
            value = re.sub(
                r"\s*\([^)]*\)\s*$",
                "",
                value,
            )

            result["bic"] = value
            break

    # fallback
    if not result["bic"]:

        for line in lines:

            match = re.search(
                r"\bBIC:\s*([A-Z0-9]{8,11})",
                line,
                re.I,
            )

            if match:
                result["bic"] = (
                    match.group(1).upper()
                )
                break

    # =====================================================
    # BANK
    # =====================================================

    result["bank"] = find_line_value(
        lines,
        [
            "Bank:",
            "Bank",
            "Banca:",
            "Banque:",
            "Banco:",
        ],
    )

    # إزالة أشياء غير مرغوبة
    if result["bank"]:
        result["bank"] = result[
            "bank"
        ].strip()

    # =====================================================
    # BRANCH
    # =====================================================

    result["branch"] = find_line_value(
        lines,
        [
            "Branch number:",
            "Branch number",
            "Branch:",
            "Branch",
            "Filiale:",
            "Oficina:",
        ],
    )

    # =====================================================
    # ADDRESS
    # =====================================================

    # الموقع عادة يضع العنوان مباشرة بعد اسم البنك
    # وقبل SEPA.

    bank_index = None

    for i, line in enumerate(lines):

        if (
            result["bank"]
            and line.strip()
            == result["bank"].strip()
        ):
            bank_index = i
            break

    if bank_index is not None:

        address_lines = []

        for line in lines[
            bank_index + 1:
        ]:

            low = line.lower()

            if (
                "sepa credit transfer"
                in low
                or "sepa direct debit"
                in low
                or "b2b is supported"
                in low
                or "sepa instant"
                in low
                or "this iban can be found"
                in low
                or "data valid as of"
                in low
                or "branch number" in low
                or "filiale:" in low
                or "oficina:" in low
            ):
                break

            # نتجنب العناوين العامة
            if (
                line.startswith("BIC:")
                or line.startswith("IBAN:")
            ):
                continue

            address_lines.append(line)

            if len(address_lines) >= 4:
                break

        if address_lines:
            result["address"] = "\n".join(
                address_lines
            )

    # =====================================================
    # SEPA CREDIT TRANSFER
    # =====================================================

    result["sepa"] = find_boolean(
        lines,
        [
            "SEPA Credit Transfer",
            "SEPA Credit Transfer is",
        ],
    )

    # =====================================================
    # SEPA DIRECT DEBIT
    # =====================================================

    result["direct_debit"] = find_boolean(
        lines,
        [
            "SEPA Direct Debit",
        ],
    )

    # =====================================================
    # B2B
    # =====================================================

    result["b2b"] = find_boolean(
        lines,
        [
            "B2B",
        ],
    )

    # =====================================================
    # SEPA INSTANT
    # =====================================================

    result["instant"] = find_boolean(
        lines,
        [
            "SEPA Instant Credit Transfer",
        ],
    )

    # =====================================================
    # DATA DATE
    # =====================================================

    for line in lines:

        if "Data valid as of:" in line:

            result["data_date"] = (
                line.split(
                    ":",
                    1
                )[1].strip()
            )

            break

    return result


# =========================================================
# ONLINE CHECK
# =========================================================

async def check_iban(
    session,
    iban,
):

    url = (
        BASE_URL
        + iban
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/139.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:

        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(
                total=30
            ),
            allow_redirects=True,
        ) as response:

            if response.status != 200:

                return {
                    "error": (
                        f"الموقع رجع HTTP "
                        f"{response.status}"
                    )
                }

            html = await response.text()

            return parse_page(
                html,
                iban,
            )

    except asyncio.TimeoutError:

        return {
            "error": "انتهت مهلة الاتصال بالموقع."
        }

    except Exception as e:

        logger.exception(
            "IBAN check error"
        )

        return {
            "error": str(e)
        }


# =========================================================
# FORMAT
# =========================================================

def supported(value):

    if value is True:
        return "✅ مدعوم"

    if value is False:
        return "❌ غير مدعوم"

    return "⚠️ غير محدد من المصدر"


def format_result(data):

    if data.get("error"):

        return (
            "⚠️ <b>تعذر الفحص</b>\n\n"
            f"<code>{data.get('iban', '')}</code>\n"
            f"• السبب: {data['error']}"
        )

    status = data.get(
        "valid"
    )

    if status is True:
        status_text = "✅ صالح"
    elif status is False:
        status_text = "❌ غير صالح"
    else:
        status_text = "⚠️ لم يتم تحديد الحالة"

    address = (
        data.get("address")
        or "غير متوفر"
    )

    # حماية HTML
    from html import escape

    bank = escape(
        data.get("bank")
        or "غير متوفر"
    )

    bic = escape(
        data.get("bic")
        or "غير متوفر"
    )

    address = escape(
        address
    ).replace(
        "\n",
        "<br>"
    )

    branch = escape(
        data.get("branch")
        or "غير متوفر"
    )

    iban = escape(
        data.get("iban")
        or ""
    )

    date = (
        data.get("data_date")
        or "غير متوفر"
    )

    return (
        "📋 <b>نتائج فحص IBAN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        f"🔢 <code>{iban}</code>\n\n"

        f"• الحالة: <b>{status_text}</b>\n\n"

        "🏦 <b>بيانات البنك</b>\n"
        f"• البنك: {bank}\n"
        f"• BIC/SWIFT: "
        f"<code>{bic}</code>\n"
        f"• العنوان: {address}\n"
        f"• Branch: <code>{branch}</code>\n\n"

        "💶 <b>SEPA</b>\n"
        f"• SEPA Credit Transfer: "
        f"{supported(data.get('sepa'))}\n"
        f"• SEPA Direct Debit: "
        f"{supported(data.get('direct_debit'))}\n"
        f"• B2B: "
        f"{supported(data.get('b2b'))}\n"
        f"• SEPA Instant Credit Transfer: "
        f"{supported(data.get('instant'))}\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        f"ℹ️ بيانات المصدر: {escape(date)}\n"
        "ℹ️ المصدر: IBAN Calculator\n"
        "⚠️ صلاحية IBAN لا تثبت أن الحساب "
        "مفتوح أو يحتوي على رصيد."
    )


# =========================================================
# TELEGRAM
# =========================================================

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
            "❌ لم أجد IBAN.\n\n"
            "أرسل IBAN واحد أو عدة IBANs."
        )

        return

    if len(ibans) > 10:

        await update.message.reply_text(
            "❌ الحد الأقصى هو 10 IBANs "
            "في الرسالة."
        )

        return

    wait = await update.message.reply_text(
        f"⏳ جاري فحص {len(ibans)} IBAN..."
    )

    async with aiohttp.ClientSession() as session:

        results = []

        for iban in ibans:

            result = await check_iban(
                session,
                iban,
            )

            results.append(
                format_result(result)
            )

            # حتى لا نرسل طلبات كثيرة بسرعة
            await asyncio.sleep(1)

    final = (
        "\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    ).join(results)

    # Telegram message limit
    if len(final) > 4000:
        final = final[:3900] + (
            "\n\n⚠️ تم اختصار النتيجة."
        )

    await wait.edit_text(
        final,
        parse_mode="HTML",
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not TOKEN:

        raise RuntimeError(
            "❌ TELEGRAM_BOT_TOKEN غير موجود "
            "في Render Environment Variables."
        )

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message,
        )
    )

    logger.info(
        "IBAN Bot started successfully."
    )

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main() 
