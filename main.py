import os
import re
import asyncio
import logging
from html import escape

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

PORT = int(os.getenv("PORT", "10000"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

BASE_URL = "https://www.ibancalculator.com/validate/"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("LEX-IBAN-BOT")


# =========================================================
# IBAN
# =========================================================

def clean_iban(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def extract_ibans(text: str):
    found = []

    pattern = r"\b[A-Z]{2}\s*[0-9]{2}(?:[\sA-Z0-9]{10,40})\b"

    for match in re.findall(pattern, text.upper()):
        iban = clean_iban(match)

        if (
            15 <= len(iban) <= 34
            and re.fullmatch(
                r"[A-Z]{2}[0-9]{2}[A-Z0-9]+",
                iban
            )
            and iban not in found
        ):
            found.append(iban)

    # Fallback
    for word in text.split():
        iban = clean_iban(word)

        if (
            15 <= len(iban) <= 34
            and re.fullmatch(
                r"[A-Z]{2}[0-9]{2}[A-Z0-9]+",
                iban
            )
            and iban not in found
        ):
            found.append(iban)

    return found


def iban_checksum_valid(iban: str) -> bool:
    try:
        rearranged = iban[4:] + iban[:4]

        numeric = ""

        for char in rearranged:
            if char.isdigit():
                numeric += char

            elif char.isalpha():
                numeric += str(ord(char) - 55)

            else:
                return False

        remainder = 0

        for i in range(0, len(numeric), 7):
            remainder = int(
                str(remainder) + numeric[i:i + 7]
            ) % 97

        return remainder == 1

    except Exception:
        return False


# =========================================================
# HTML
# =========================================================

def get_clean_soup(html: str):
    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    for tag in soup.find_all(
        ["script", "style", "noscript", "svg"]
    ):
        tag.decompose()

    return soup


def clean_value(value):
    if not value:
        return None

    value = re.sub(
        r"\s+",
        " ",
        value
    ).strip()

    value = value.strip(
        " \t\r\n:|-"
    )

    return value or None


def is_bad_value(value):
    if not value:
        return True

    low = value.lower().strip()

    bad_values = {
        "",
        "-",
        "---",
        "not found",
        "not available",
        "unknown",
        "nicht verfügbar",
        "nicht gefunden",
        "غير متوفر",
    }

    if low in bad_values:
        return True

    bad_fragments = [
        "bankleitzahl",
        "bank code",
        "branch code",
        "sort code",
        "clearing code",
        "bank identifier",
    ]

    if any(
        x in low
        for x in bad_fragments
    ):
        return True

    return False


def get_label_value(
    soup,
    labels
):
    labels_lower = {
        x.lower().strip()
        for x in labels
    }

    # -----------------------------------------------------
    # TABLE
    # -----------------------------------------------------

    for row in soup.find_all("tr"):

        cells = row.find_all(
            ["th", "td"]
        )

        if len(cells) < 2:
            continue

        label = clean_value(
            cells[0].get_text(
                " ",
                strip=True
            )
        )

        if not label:
            continue

        if label.lower().rstrip(":") in labels_lower:

            value = clean_value(
                cells[1].get_text(
                    " ",
                    strip=True
                )
            )

            if not is_bad_value(value):
                return value

    # -----------------------------------------------------
    # LABEL / DT
    # -----------------------------------------------------

    for element in soup.find_all(
        ["label", "dt"]
    ):

        label = clean_value(
            element.get_text(
                " ",
                strip=True
            )
        )

        if not label:
            continue

        if label.lower().rstrip(":") not in labels_lower:
            continue

        sibling = element.find_next_sibling()

        if sibling:

            value = clean_value(
                sibling.get_text(
                    " ",
                    strip=True
                )
            )

            if not is_bad_value(value):
                return value

    # -----------------------------------------------------
    # STRONG / B
    # -----------------------------------------------------

    for element in soup.find_all(
        ["strong", "b"]
    ):

        label = clean_value(
            element.get_text(
                " ",
                strip=True
            )
        )

        if not label:
            continue

        if label.lower().rstrip(":") not in labels_lower:
            continue

        parent = element.parent

        if parent:

            text = clean_value(
                parent.get_text(
                    " ",
                    strip=True
                )
            )

            if ":" in text:

                value = clean_value(
                    text.split(
                        ":",
                        1
                    )[1]
                )

                if not is_bad_value(value):
                    return value

    # -----------------------------------------------------
    # EXACT LABEL: VALUE
    # -----------------------------------------------------

    for element in soup.find_all(
        ["p", "div", "li", "span"]
    ):

        text = clean_value(
            element.get_text(
                " ",
                strip=True
            )
        )

        if not text:
            continue

        for label in labels:

            pattern = (
                r"^"
                + re.escape(label)
                + r"\s*:\s*(.+)$"
            )

            match = re.match(
                pattern,
                text,
                re.IGNORECASE
            )

            if match:

                value = clean_value(
                    match.group(1)
                )

                if not is_bad_value(value):
                    return value

    return None


# =========================================================
# BANK
# =========================================================

def extract_bank(soup):

    value = get_label_value(
        soup,
        [
            "Bank",
            "Bank name",
            "Bank Name",
            "Bankname",
            "Banca",
            "Banco",
            "Banque",
        ]
    )

    if is_bad_value(value):
        return None

    return value


# =========================================================
# BIC
# =========================================================

def extract_bic(soup):

    value = get_label_value(
        soup,
        [
            "BIC",
            "BIC/SWIFT",
            "SWIFT",
        ]
    )

    if not value:
        return None

    match = re.search(
        r"\b[A-Z0-9]{8}(?:[A-Z0-9]{3})?\b",
        value.upper()
    )

    if not match:
        return None

    bic = match.group(0)

    if not re.search(
        r"[A-Z]",
        bic
    ):
        return None

    return bic


# =========================================================
# BRANCH
# =========================================================

def extract_branch(soup):

    value = get_label_value(
        soup,
        [
            "Branch number",
            "Branch Number",
            "Branch",
        ]
    )

    if is_bad_value(value):
        return None

    return value


# =========================================================
# ADDRESS
# =========================================================

def extract_address(soup):

    value = get_label_value(
        soup,
        [
            "Address",
            "Bank address",
            "Bank Address",
        ]
    )

    if not is_bad_value(value):
        return value

    bank = extract_bank(soup)

    if not bank:
        return None

    elements = soup.find_all(
        ["p", "div", "td", "li"]
    )

    for i, element in enumerate(elements):

        current = clean_value(
            element.get_text(
                " ",
                strip=True
            )
        )

        if current != bank:
            continue

        possible = []

        for next_element in elements[
            i + 1:i + 5
        ]:

            text = clean_value(
                next_element.get_text(
                    " ",
                    strip=True
                )
            )

            if not text:
                continue

            low = text.lower()

            if (
                "sepa credit transfer"
                in low
                or "sepa direct debit"
                in low
                or "sepa instant"
                in low
                or "b2b"
                in low
                or "branch number"
                in low
            ):
                break

            if text == bank:
                continue

            possible.append(text)

        if possible:
            return "\n".join(
                possible[:3]
            )

    return None


# =========================================================
# SEPA
# =========================================================

def detect_support(
    soup,
    phrases
):
    text = soup.get_text(
        " ",
        strip=True
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    ).lower()

    for phrase in phrases:

        p = re.escape(
            phrase.lower()
        )

        # Supported
        if re.search(
            p
            + r"\s+(?:is\s+)?supported\b",
            text,
            re.IGNORECASE
        ):
            return True

        # Not supported
        if re.search(
            p
            + r"\s+(?:is\s+)?not\s+supported\b",
            text,
            re.IGNORECASE
        ):
            return False

        # German
        if re.search(
            p
            + r".{0,40}"
            + r"(nicht unterstützt|nicht unterstuetzt)",
            text,
            re.IGNORECASE
        ):
            return False

        if re.search(
            p
            + r".{0,40}"
            + r"(unterstützt|unterstuetzt)",
            text,
            re.IGNORECASE
        ):
            return True

    return None


# =========================================================
# PARSE
# =========================================================

def parse_result(
    html,
    iban
):
    soup = get_clean_soup(
        html
    )

    text = soup.get_text(
        "\n",
        strip=True
    )

    lower = text.lower()

    result = {
        "iban": iban,
        "valid": None,
        "country": iban[:2],
        "bank": None,
        "bic": None,
        "address": None,
        "branch": None,
        "sepa": None,
        "direct_debit": None,
        "b2b": None,
        "instant": None,
    }

    # -----------------------------------------------------
    # VALID
    # -----------------------------------------------------

    if (
        "this is a valid iban" in lower
        or "this iban is valid" in lower
        or "is a valid iban" in lower
        or "dies ist eine gültige iban" in lower
    ):
        result["valid"] = True

    elif (
        "this is not a valid iban" in lower
        or "this iban is invalid" in lower
        or "this is an invalid iban" in lower
        or "dies ist keine gültige iban" in lower
    ):
        result["valid"] = False

    # -----------------------------------------------------
    # BANK
    # -----------------------------------------------------

    result["bank"] = extract_bank(
        soup
    )

    # -----------------------------------------------------
    # BIC
    # -----------------------------------------------------

    result["bic"] = extract_bic(
        soup
    )

    # -----------------------------------------------------
    # BRANCH
    # -----------------------------------------------------

    result["branch"] = extract_branch(
        soup
    )

    # -----------------------------------------------------
    # ADDRESS
    # -----------------------------------------------------

    result["address"] = extract_address(
        soup
    )

    # -----------------------------------------------------
    # SEPA
    # -----------------------------------------------------

    result["sepa"] = detect_support(
        soup,
        [
            "SEPA Credit Transfer"
        ]
    )

    result["direct_debit"] = detect_support(
        soup,
        [
            "SEPA Direct Debit"
        ]
    )

    result["b2b"] = detect_support(
        soup,
        [
            "B2B"
        ]
    )

    result["instant"] = detect_support(
        soup,
        [
            "SEPA Instant Credit Transfer"
        ]
    )

    return result


# =========================================================
# CHECK WEBSITE
# =========================================================

async def check_iban(
    session,
    iban
):
    url = BASE_URL + iban

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
            allow_redirects=True
        ) as response:

            if response.status != 200:

                return {
                    "iban": iban,
                    "error": (
                        f"HTTP {response.status}"
                    )
                }

            html = await response.text()

            return parse_result(
                html,
                iban
            )

    except asyncio.TimeoutError:

        return {
            "iban": iban,
            "error": "انتهت مهلة الاتصال."
        }

    except Exception:

        logger.exception(
            "IBAN request failed"
        )

        return {
            "iban": iban,
            "error": (
                "حدث خطأ أثناء الاتصال بالمصدر."
            )
        }


# =========================================================
# FORMAT
# =========================================================

def support_text(value):

    if value is True:
        return "✅ مدعوم"

    if value is False:
        return "❌ غير مدعوم"

    return "⚠️ غير محدد"


def format_result(data):

    iban = escape(
        data.get("iban", "")
    )

    if data.get("error"):

        return (
            "📋 <b>نتائج فحص IBAN</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔢 <code>{iban}</code>\n\n"
            "⚠️ تعذر الحصول على النتيجة.\n"
            f"• السبب: {escape(data['error'])}\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "By LEX"
        )

    if data.get("valid") is True:

        status = "✅ صالح"

    elif data.get("valid") is False:

        status = "❌ غير صالح"

    else:

        if iban_checksum_valid(
            data["iban"]
        ):
            status = "⚠️ صالح فنيًا"
        else:
            status = "❌ غير صالح فنيًا"

    bank = escape(
        data.get("bank")
        or "غير متوفر"
    )

    bic = escape(
        data.get("bic")
        or "غير متوفر"
    )

    branch = escape(
        data.get("branch")
        or "غير متوفر"
    )

    address = (
        data.get("address")
        or "غير متوفر"
    )

    address = escape(
        address
    ).replace(
        "\n",
        "<br>"
    )

    country = escape(
        data.get("country")
        or "غير متوفر"
    )

    return (
        "📋 <b>نتائج فحص IBAN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        f"🔢 <code>{iban}</code>\n\n"

        f"• الحالة: <b>{status}</b>\n"
        f"• الدولة: <b>{country}</b>\n\n"

        "🏦 <b>بيانات البنك</b>\n"
        f"• البنك: {bank}\n"
        f"• BIC/SWIFT: "
        f"<code>{bic}</code>\n"
        f"• العنوان: {address}\n"
        f"• Branch: "
        f"<code>{branch}</code>\n\n"

        "💶 <b>SEPA</b>\n"
        f"• SEPA Credit Transfer: "
        f"{support_text(data.get('sepa'))}\n"
        f"• SEPA Direct Debit: "
        f"{support_text(data.get('direct_debit'))}\n"
        f"• B2B: "
        f"{support_text(data.get('b2b'))}\n"
        f"• SEPA Instant Credit Transfer: "
        f"{support_text(data.get('instant'))}\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "By LEX"
    )


# =========================================================
# TELEGRAM HANDLER
# =========================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
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
            "❌ لم أجد IBAN صالحًا.\n\n"
            "أرسل IBAN واحد أو قائمة IBANs."
        )

        return

    if len(ibans) > 10:

        await update.message.reply_text(
            "❌ الحد الأقصى هو 10 IBANs."
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
                iban
            )

            results.append(
                format_result(result)
            )

            await asyncio.sleep(1)

    final_text = (
        "\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    ).join(results)

    if len(final_text) > 4000:

        final_text = (
            final_text[:3900]
            + "\n\n⚠️ تم اختصار النتائج."
        )

    try:

        await wait.edit_text(
            final_text,
            parse_mode="HTML"
        )

    except Exception:

        await update.message.reply_text(
            final_text,
            parse_mode="HTML"
        )


# =========================================================
# START WEBHOOK
# =========================================================

def main():

    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN غير موجود "
            "في Environment Variables."
        )

    if not RENDER_EXTERNAL_URL:
        raise RuntimeError(
            "RENDER_EXTERNAL_URL غير موجود."
        )

    webhook_url = (
        RENDER_EXTERNAL_URL.rstrip("/")
        + "/telegram"
    )

    logger.info(
        "Starting LEX IBAN bot..."
    )

    logger.info(
        "Webhook URL: %s",
        webhook_url
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
            handle_message
        )
    )

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="telegram",
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main() 
