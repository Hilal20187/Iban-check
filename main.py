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
# CONFIG
# =========================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

SITE_URL = "https://www.ibancalculator.com/iban_validieren.html"

TIMEOUT = 30

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# IBAN CLEAN
# =========================================================

def clean_iban(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def extract_ibans(text: str):
    found = []

    # IBANs with or without spaces
    candidates = re.findall(
        r"\b[A-Z]{2}[0-9A-Z][A-Z0-9\s]{12,40}\b",
        text.upper()
    )

    for candidate in candidates:

        iban = clean_iban(candidate)

        if (
            len(iban) >= 15
            and re.match(r"^[A-Z]{2}[0-9]{2}", iban)
            and iban not in found
        ):
            found.append(iban)

    # fallback for single IBAN
    for word in text.split():

        iban = clean_iban(word)

        if (
            len(iban) >= 15
            and re.match(r"^[A-Z]{2}[0-9]{2}", iban)
            and iban not in found
        ):
            found.append(iban)

    return found


# =========================================================
# MOD 97
# =========================================================

def check_mod97(iban: str) -> bool:

    rearranged = iban[4:] + iban[:4]

    numeric = ""

    for char in rearranged:

        if char.isdigit():
            numeric += char
        else:
            numeric += str(ord(char) - 55)

    remainder = 0

    for i in range(0, len(numeric), 7):

        remainder = int(
            str(remainder) + numeric[i:i + 7]
        ) % 97

    return remainder == 1


# =========================================================
# SCRAPE IBAN CALCULATOR
# =========================================================

async def check_iban_online(
    session,
    iban: str,
):

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/130.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    # The website accepts the IBAN as a query parameter
    # used by its validation page.
    params = {
        "tx_valIBAN_pi1[iban]": iban,
    }

    try:

        async with session.get(
            SITE_URL,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(
                total=TIMEOUT
            ),
        ) as response:

            if response.status != 200:

                return {
                    "online": False,
                    "error": (
                        f"HTTP {response.status}"
                    ),
                }

            html = await response.text()

    except Exception as e:

        logger.exception(
            "Website request failed"
        )

        return {
            "online": False,
            "error": str(e),
        }

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    # Remove scripts/styles
    for tag in soup(
        ["script", "style", "noscript"]
    ):
        tag.decompose()

    text = soup.get_text(
        "\n",
        strip=True
    )

    # -----------------------------------------------------
    # Extract useful fields
    # -----------------------------------------------------

    result = {
        "online": True,
        "valid": None,
        "bank": None,
        "bic": None,
        "city": None,
        "address": None,
        "country": None,
        "sepa": None,
        "instant": None,
        "raw_text": text,
    }

    lower = text.lower()

    # -----------------------------------------------------
    # VALID / INVALID
    # -----------------------------------------------------

    invalid_words = [
        "invalid iban",
        "iban is invalid",
        "not valid",
        "incorrect iban",
        "ungültig",
        "invalid",
    ]

    valid_words = [
        "valid iban",
        "iban is valid",
        "valid",
        "gültig",
    ]

    if any(
        word in lower
        for word in invalid_words
    ):
        result["valid"] = False

    elif any(
        word in lower
        for word in valid_words
    ):
        result["valid"] = True

    # -----------------------------------------------------
    # Search labels in HTML/text
    # -----------------------------------------------------

    def find_after_labels(
        labels,
        max_distance=300,
    ):

        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip()
        ]

        for i, line in enumerate(lines):

            line_lower = line.lower()

            for label in labels:

                if label in line_lower:

                    # Same line: "Bank: XYZ"
                    if ":" in line:

                        value = line.split(
                            ":",
                            1
                        )[1].strip()

                        if value:
                            return value

                    # Next few lines
                    for j in range(
                        i + 1,
                        min(
                            i + 4,
                            len(lines)
                        )
                    ):

                        value = lines[j].strip()

                        if (
                            value
                            and len(value)
                            < max_distance
                        ):
                            return value

        return None

    result["bank"] = find_after_labels(
        [
            "bank name",
            "bank",
            "bankname",
        ]
    )

    result["bic"] = find_after_labels(
        [
            "bic",
            "swift",
            "bic/swift",
        ]
    )

    result["city"] = find_after_labels(
        [
            "city",
            "town",
        ]
    )

    result["address"] = find_after_labels(
        [
            "address",
            "bank address",
        ]
    )

    # -----------------------------------------------------
    # SEPA
    # -----------------------------------------------------

    if (
        "sepa instant" in lower
        or "instant credit transfer" in lower
        or "sct inst" in lower
    ):

        result["instant"] = True

    if (
        "sepa credit transfer" in lower
        or "sepa transfer" in lower
        or "sct" in lower
    ):

        result["sepa"] = True

    return result


# =========================================================
# RESULT FORMAT
# =========================================================

def format_result(
    iban,
    online_result,
):

    country = iban[:2]

    try:
        technical_valid = check_mod97(
            iban
        )
    except Exception:
        technical_valid = False

    # Website result takes priority
    website_valid = online_result.get(
        "valid"
    )

    if website_valid is False:
        status = "❌ غير صالح"

    elif website_valid is True:
        status = "✅ صالح"

    elif technical_valid:
        status = (
            "✅ صالح من ناحية التحقق الفني"
        )

    else:
        status = "❌ غير صالح"

    def value(key):
        return (
            online_result.get(key)
            or "غير متوفر"
        )

    sepa = online_result.get(
        "sepa"
    )

    instant = online_result.get(
        "instant"
    )

    if sepa is True:
        sepa_text = "✅ مدعوم"
    elif sepa is False:
        sepa_text = "❌ غير مدعوم"
    else:
        sepa_text = "⚠️ غير محدد"

    if instant is True:
        instant_text = "✅ مدعوم"
    elif instant is False:
        instant_text = "❌ غير مدعوم"
    else:
        instant_text = "⚠️ غير محدد"

    return (
        "📋 <b>نتائج فحص IBAN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        f"🔢 <code>{iban}</code>\n\n"

        f"• الحالة: <b>{status}</b>\n"
        f"• الدولة: <code>{country}</code>\n\n"

        "🏦 <b>بيانات البنك</b>\n"
        f"• البنك: {value('bank')}\n"
        f"• BIC/SWIFT: "
        f"<code>{value('bic')}</code>\n"
        f"• المدينة: {value('city')}\n"
        f"• العنوان: {value('address')}\n\n"

        "💶 <b>SEPA</b>\n"
        f"• SEPA Normal (SCT): "
        f"{sepa_text}\n"
        f"• SEPA Instant (SCT Inst): "
        f"{instant_text}\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "ℹ️ المصدر: IBAN Calculator\n"
        "ℹ️ التحقق لا يثبت أن الحساب مفتوح "
        "أو يحتوي على رصيد."
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
        update.message.text or ""
    ).strip()

    ibans = extract_ibans(text)

    if not ibans:

        await update.message.reply_text(
            "❌ أرسل IBAN صالحًا."
        )

        return

    if len(ibans) > 10:

        await update.message.reply_text(
            "❌ الحد الأقصى 10 IBAN في الرسالة."
        )

        return

    wait = (
        await update.message.reply_text(
            f"⏳ جاري فحص {len(ibans)} IBAN..."
        )
    )

    async with aiohttp.ClientSession() as session:

        results = []

        for iban in ibans:

            result = await check_iban_online(
                session,
                iban
            )

            results.append(
                format_result(
                    iban,
                    result
                )
            )

            # لا نضغط على الموقع بسرعة
            await asyncio.sleep(1)

    final = (
        "\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    ).join(results)

    if len(final) > 4000:

        final = final[:3950] + (
            "\n\n⚠️ تم اختصار النتيجة."
        )

    try:

        await wait.edit_text(
            final,
            parse_mode="HTML"
        )

    except Exception:

        await update.message.reply_text(
            final,
            parse_mode="HTML"
        )


# =========================================================
# MAIN
# =========================================================

def main():

    if not TOKEN:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN غير موجود في Environment Variables."
        )

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    print(
        "IBAN Telegram Bot started..."
    )

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
