import os
import re
import asyncio
import logging
from html import escape
from http import HTTPStatus

import aiohttp
from bs4 import BeautifulSoup
import uvicorn

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================================================
# CONFIGURATION
# =========================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

WEBHOOK_SECRET = os.getenv(
    "TELEGRAM_WEBHOOK_SECRET",
    ""
)

BASE_URL = "https://www.ibancalculator.com/validate/"

# Delete messages after 20 seconds
DELETE_AFTER = 20


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("LEX-IBAN-BOT")


# =========================================================
# IBAN FUNCTIONS
# =========================================================

def clean_iban(value: str) -> str:

    return re.sub(
        r"[^A-Z0-9]",
        "",
        value.upper()
    )


def extract_ibans(text: str):

    found = []

    pattern = (
        r"\b[A-Z]{2}\s*[0-9]{2}"
        r"(?:[\sA-Z0-9]{10,40})\b"
    )

    for match in re.findall(
        pattern,
        text.upper()
    ):

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

                numeric += str(
                    ord(char) - 55
                )

            else:

                return False

        remainder = 0

        for i in range(
            0,
            len(numeric),
            7
        ):

            remainder = int(
                str(remainder)
                + numeric[i:i + 7]
            ) % 97

        return remainder == 1

    except Exception:

        return False


# =========================================================
# HTML HELPERS
# =========================================================

def get_clean_soup(html: str):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    for tag in soup.find_all(
        [
            "script",
            "style",
            "noscript",
            "svg"
        ]
    ):

        tag.decompose()

    return soup


def clean_value(value):

    if not value:
        return None

    value = re.sub(
        r"[ \t]+",
        " ",
        value
    ).strip()

    value = value.strip(
        " \t\r\n:|-"
    )

    return value or None


def normalize_text(value):

    if not value:
        return ""

    return re.sub(
        r"\s+",
        " ",
        value
    ).strip().lower()


def is_bad_value(value):

    if not value:
        return True

    low = normalize_text(value)

    bad_values = {
        "",
        "-",
        "--",
        "---",
        "not found",
        "not available",
        "unknown",
        "n/a",
        "na",
        "none",
        "nicht verfügbar",
        "nicht gefunden",
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

    for item in bad_fragments:

        if item in low:

            return True

    return False


# =========================================================
# GENERIC LABEL PARSER
# =========================================================

def get_label_value(
    soup,
    labels
):

    labels_lower = {
        normalize_text(label).rstrip(":")
        for label in labels
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

        value = clean_value(
            cells[1].get_text(
                " ",
                strip=True
            )
        )

        if not label:

            continue

        label_normalized = normalize_text(
            label
        ).rstrip(":")

        if (
            label_normalized in labels_lower
            and not is_bad_value(value)
        ):

            return value

    # -----------------------------------------------------
    # DT / LABEL
    # -----------------------------------------------------

    for element in soup.find_all(
        ["dt", "label"]
    ):

        label = clean_value(
            element.get_text(
                " ",
                strip=True
            )
        )

        if not label:

            continue

        label_normalized = normalize_text(
            label
        ).rstrip(":")

        if label_normalized not in labels_lower:

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

        label_normalized = normalize_text(
            label
        ).rstrip(":")

        if label_normalized not in labels_lower:

            continue

        parent = element.parent

        if not parent:

            continue

        text = clean_value(
            parent.get_text(
                " ",
                strip=True
            )
        )

        if not text or ":" not in text:

            continue

        value = clean_value(
            text.split(
                ":",
                1
            )[1]
        )

        if not is_bad_value(value):

            return value

    # -----------------------------------------------------
    # LABEL: VALUE
    # -----------------------------------------------------

    for element in soup.find_all(
        [
            "p",
            "div",
            "li",
            "span",
            "td"
        ]
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

    if not is_bad_value(value):

        return value

    for element in soup.find_all(
        [
            "p",
            "div",
            "td",
            "li",
            "span"
        ]
    ):

        text = clean_value(
            element.get_text(
                " ",
                strip=True
            )
        )

        if not text:

            continue

        match = re.match(
            r"^Bank\s*:\s*(.+)$",
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
# BIC / SWIFT
# =========================================================

def extract_bic(soup):

    value = get_label_value(
        soup,
        [
            "BIC",
            "BIC/SWIFT",
            "BIC / SWIFT",
            "SWIFT",
            "SWIFT/BIC",
        ]
    )

    if value:

        match = re.search(
            r"\b[A-Z0-9]{8}(?:[A-Z0-9]{3})?\b",
            value.upper()
        )

        if match:

            return match.group(0)

    text = soup.get_text(
        " ",
        strip=True
    )

    matches = re.findall(
        r"\b[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b",
        text.upper()
    )

    for bic in matches:

        if len(bic) in (8, 11):

            return bic

    return None


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
            "Branch code",
            "Branch Code",
        ]
    )

    if not is_bad_value(value):

        return value

    for element in soup.find_all(
        [
            "p",
            "div",
            "td",
            "li",
            "span"
        ]
    ):

        text = clean_value(
            element.get_text(
                " ",
                strip=True
            )
        )

        if not text:

            continue

        match = re.match(
            r"^(?:Branch|Branch Number|Branch Code)"
            r"\s*:\s*(.+)$",
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
# ADDRESS
# =========================================================

def extract_address(soup):

    # -----------------------------------------------------
    # Explicit address field
    # -----------------------------------------------------

    value = get_label_value(
        soup,
        [
            "Address",
            "Bank address",
            "Bank Address",
            "Bankaddress",
            "Adresse",
            "Bankadresse",
        ]
    )

    if not is_bad_value(value):

        return value

    bank = extract_bank(soup)

    if not bank:

        return None

    bank_normalized = normalize_text(
        bank
    )

    # -----------------------------------------------------
    # Search after bank name
    # -----------------------------------------------------

    elements = soup.find_all(
        [
            "td",
            "p",
            "li",
            "div"
        ]
    )

    for i, element in enumerate(elements):

        current = clean_value(
            element.get_text(
                " ",
                strip=True
            )
        )

        if not current:

            continue

        if normalize_text(
            current
        ) != bank_normalized:

            continue

        address_lines = []

        for next_element in elements[
            i + 1:i + 10
        ]:

            text = clean_value(
                next_element.get_text(
                    " ",
                    strip=True
                )
            )

            if not text:

                continue

            low = normalize_text(
                text
            )

            # Stop at other sections
            if (
                "sepa credit transfer" in low
                or "sepa direct debit" in low
                or "sepa instant credit transfer" in low
                or "instant credit transfer" in low
                or low == "b2b"
                or low.startswith("b2b ")
                or low.startswith("bic:")
                or low.startswith("bic/swift:")
                or low.startswith("swift:")
                or low.startswith("branch:")
                or low.startswith("branch number:")
                or low.startswith("branch code:")
            ):

                break

            # Duplicate bank
            if low == bank_normalized:

                continue

            # BIC
            if re.fullmatch(
                r"[A-Z0-9]{8}(?:[A-Z0-9]{3})?",
                text.upper()
            ):

                continue

            # Labels
            if low.rstrip(":") in {
                "bank",
                "bank name",
                "address",
                "bank address",
                "bic",
                "bic/swift",
                "swift",
                "branch",
                "branch number",
                "branch code",
            }:

                continue

            # UI text
            if low in {
                "copy",
                "copy to clipboard",
                "validate another iban",
                "validate iban",
                "check another iban",
            }:

                continue

            if len(text) > 180:

                continue

            address_lines.append(
                text
            )

            if len(address_lines) >= 3:

                break

        if address_lines:

            return "\n".join(
                address_lines
            )

    # -----------------------------------------------------
    # Postal code fallback
    # -----------------------------------------------------

    page_text = soup.get_text(
        "\n",
        strip=True
    )

    lines = []

    for line in page_text.splitlines():

        line = clean_value(
            line
        )

        if line:

            lines.append(line)

    postal_pattern = re.compile(
        r"\b\d{4,5}\s+[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]+\b"
    )

    for index, line in enumerate(lines):

        if postal_pattern.search(line):

            address_lines = []

            for p in range(
                max(0, index - 2),
                index
            ):

                candidate = lines[p]

                if (
                    normalize_text(candidate)
                    != bank_normalized
                    and len(candidate) < 180
                ):

                    address_lines.append(
                        candidate
                    )

            address_lines.append(
                line
            )

            if address_lines:

                return "\n".join(
                    address_lines[-3:]
                )

    return None


# =========================================================
# SEPA SUPPORT
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

        # NOT SUPPORTED FIRST
        if re.search(
            p
            + r"\s+(?:is\s+)?not\s+supported\b",
            text,
            re.IGNORECASE
        ):

            return False

        if re.search(
            p
            + r".{0,50}"
            + r"(nicht unterstützt|nicht unterstuetzt)",
            text,
            re.IGNORECASE
        ):

            return False

        # SUPPORTED
        if re.search(
            p
            + r"\s+(?:is\s+)?supported\b",
            text,
            re.IGNORECASE
        ):

            return True

        if re.search(
            p
            + r".{0,50}"
            + r"(unterstützt|unterstuetzt)",
            text,
            re.IGNORECASE
        ):

            return True

    return None


# =========================================================
# PARSE RESULT
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
    # Validity
    # -----------------------------------------------------

    if (
        "this is a valid iban" in lower
        or "this iban is valid" in lower
        or "is a valid iban" in lower
        or "valid iban" in lower
        or "dies ist eine gültige iban" in lower
    ):

        result["valid"] = True

    elif (
        "this is not a valid iban" in lower
        or "this iban is invalid" in lower
        or "this is an invalid iban" in lower
        or "invalid iban" in lower
        or "dies ist keine gültige iban" in lower
    ):

        result["valid"] = False

    # -----------------------------------------------------
    # Bank information
    # -----------------------------------------------------

    result["bank"] = extract_bank(
        soup
    )

    result["bic"] = extract_bic(
        soup
    )

    result["branch"] = extract_branch(
        soup
    )

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
            "SEPA Instant Credit Transfer",
            "SEPA Instant",
        ]
    )

    return result


# =========================================================
# CHECK IBAN
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
        "Accept": (
            "text/html,"
            "application/xhtml+xml,"
            "application/xml;q=0.9,"
            "*/*;q=0.8"
        ),
        "Accept-Language": (
            "en-US,en;q=0.9"
        ),
        "Connection": "keep-alive",
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
            "error": "Request timeout."
        }

    except Exception as error:

        logger.exception(
            "IBAN request failed: %s",
            error
        )

        return {
            "iban": iban,
            "error": (
                "Unable to contact "
                "the IBAN source."
            )
        }


# =========================================================
# RESULT FORMAT
# =========================================================

def support_text(value):

    if value is True:

        return "✅ Supported"

    if value is False:

        return "❌ Not supported"

    return "⚠️ Unknown"


def format_result(data):

    iban = escape(
        str(
            data.get("iban", "")
        )
    )

    if data.get("error"):

        return (
            "📋 <b>IBAN Check Result</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"

            f"🔢 <code>{iban}</code>\n\n"

            "⚠️ Unable to get the result.\n"
            f"• Reason: "
            f"{escape(str(data['error']))}\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "By LEX"
        )

    # -----------------------------------------------------
    # Status
    # -----------------------------------------------------

    if data.get("valid") is True:

        status = "Valid"

    elif data.get("valid") is False:

        status = "Invalid"

    else:

        if iban_checksum_valid(
            data["iban"]
        ):

            status = "Technically valid"

        else:

            status = "Technically invalid"

    # -----------------------------------------------------
    # Bank
    # -----------------------------------------------------

    bank = escape(
        str(
            data.get("bank")
            or "Not available"
        )
    )

    # -----------------------------------------------------
    # BIC
    # -----------------------------------------------------

    bic = escape(
        str(
            data.get("bic")
            or "Not available"
        )
    )

    # -----------------------------------------------------
    # Branch
    # -----------------------------------------------------

    branch = escape(
        str(
            data.get("branch")
            or "Not available"
        )
    )

    # -----------------------------------------------------
    # Country
    # -----------------------------------------------------

    country = escape(
        str(
            data.get("country")
            or "Not available"
        )
    )

    # -----------------------------------------------------
    # Address
    # -----------------------------------------------------

    address = data.get(
        "address"
    )

    if address:

        # IMPORTANT:
        # Telegram supports \n.
        # Do NOT use <br>.

        address = escape(
            str(address)
        )

    else:

        address = "Not available"

    # -----------------------------------------------------
    # Final message
    # -----------------------------------------------------

    return (
        "📋 <b>IBAN Check Result</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        f"🔢 <code>{iban}</code>\n\n"

        f"• Status: <b>{status}</b>\n"
        f"• Country: <b>{country}</b>\n\n"

        "🏦 <b>Bank Information</b>\n"

        f"• Bank: {bank}\n"

        f"• BIC/SWIFT: "
        f"<code>{bic}</code>\n"

        f"• Address: {address}\n"

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
# DELETE MESSAGES
# =========================================================

async def delete_messages_after_delay(
    bot,
    chat_id,
    message_ids,
    delay=DELETE_AFTER
):

    # Wait 20 seconds
    await asyncio.sleep(
        delay
    )

    for message_id in message_ids:

        if not message_id:
            continue

        try:

            await bot.delete_message(
                chat_id=chat_id,
                message_id=message_id
            )

            logger.info(
                "Deleted message %s from chat %s",
                message_id,
                chat_id
            )

        except BadRequest as error:

            logger.warning(
                "Could not delete message %s: %s",
                message_id,
                error
            )

        except Exception as error:

            logger.exception(
                "Delete error for message %s: %s",
                message_id,
                error
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

    # -----------------------------------------------------
    # Extract IBANs
    # -----------------------------------------------------

    ibans = extract_ibans(
        text
    )

    if not ibans:

        return

    # -----------------------------------------------------
    # Save user's message ID
    # -----------------------------------------------------

    user_message_id = (
        update.message.message_id
    )

    chat_id = (
        update.message.chat_id
    )

    # -----------------------------------------------------
    # Start checking
    # -----------------------------------------------------

    wait_message = None

    try:

        wait_message = (
            await update.message.reply_text(
                f"⏳ Checking {len(ibans)} IBAN(s)..."
            )
        )

    except Exception as error:

        logger.exception(
            "Could not send checking message: %s",
            error
        )

        return

    # -----------------------------------------------------
    # Check IBANs
    # -----------------------------------------------------

    async with aiohttp.ClientSession() as session:

        results = []

        for iban in ibans:

            try:

                result = await check_iban(
                    session,
                    iban
                )

                results.append(
                    format_result(result)
                )

            except Exception as error:

                logger.exception(
                    "Processing error: %s",
                    error
                )

                results.append(
                    (
                        "📋 <b>IBAN Check Result</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"🔢 <code>{escape(iban)}</code>\n\n"
                        "⚠️ Error while processing.\n\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "By LEX"
                    )
                )

            await asyncio.sleep(1)

    # -----------------------------------------------------
    # Build final result
    # -----------------------------------------------------

    final_text = (
        "\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    ).join(
        results
    )

    if len(final_text) > 4000:

        final_text = (
            final_text[:3900]
            + "\n\n⚠️ Results truncated."
        )

    # -----------------------------------------------------
    # Edit checking message
    # -----------------------------------------------------

    bot_result_message_id = None

    try:

        await wait_message.edit_text(
            final_text,
            parse_mode="HTML"
        )

        bot_result_message_id = (
            wait_message.message_id
        )

    except Exception as error:

        logger.exception(
            "Could not edit result message: %s",
            error
        )

        try:

            result_message = (
                await update.message.reply_text(
                    final_text,
                    parse_mode="HTML"
                )
            )

            bot_result_message_id = (
                result_message.message_id
            )

            # Delete old checking message too
            try:

                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=wait_message.message_id
                )

            except Exception:

                pass

        except Exception as error2:

            logger.exception(
                "Could not send result message: %s",
                error2
            )

            return

    # -----------------------------------------------------
    # DELETE USER MESSAGE + BOT RESULT
    # AFTER 20 SECONDS
    # -----------------------------------------------------

    asyncio.create_task(
        delete_messages_after_delay(
            context.bot,
            chat_id,
            [
                user_message_id,
                bot_result_message_id,
            ],
            DELETE_AFTER
        )
    )


# =========================================================
# WEBHOOK SERVER
# =========================================================

application = None


async def telegram_webhook(
    request: Request
):

    if WEBHOOK_SECRET:

        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            ""
        )

        if received_secret != WEBHOOK_SECRET:

            return PlainTextResponse(
                "Unauthorized",
                status_code=HTTPStatus.UNAUTHORIZED
            )

    try:

        data = await request.json()

        update = Update.de_json(
            data=data,
            bot=application.bot
        )

        await application.update_queue.put(
            update
        )

        return PlainTextResponse(
            "OK",
            status_code=HTTPStatus.OK
        )

    except Exception as error:

        logger.exception(
            "Webhook error: %s",
            error
        )

        return PlainTextResponse(
            "Bad Request",
            status_code=HTTPStatus.BAD_REQUEST
        )


# =========================================================
# HEALTH CHECK
# =========================================================

async def health_check(
    request: Request
):

    return PlainTextResponse(
        "LEX IBAN Bot is running.",
        status_code=HTTPStatus.OK
    )


async def root(
    request: Request
):

    return PlainTextResponse(
        "LEX IBAN Bot",
        status_code=HTTPStatus.OK
    )


# =========================================================
# APPLICATION STARTUP
# =========================================================

async def run_server():

    global application

    # -----------------------------------------------------
    # Environment checks
    # -----------------------------------------------------

    if not TOKEN:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set."
        )

    if not RENDER_EXTERNAL_URL:

        raise RuntimeError(
            "RENDER_EXTERNAL_URL is not set."
        )

    webhook_url = (
        RENDER_EXTERNAL_URL.rstrip("/")
        + "/telegram"
    )

    # -----------------------------------------------------
    # Telegram application
    # -----------------------------------------------------

    application = (
        Application.builder()
        .token(TOKEN)
        .updater(None)
        .build()
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message
        )
    )

    # -----------------------------------------------------
    # Starlette routes
    # -----------------------------------------------------

    routes = [
        Route(
            "/",
            root,
            methods=["GET"]
        ),

        Route(
            "/health",
            health_check,
            methods=["GET"]
        ),

        Route(
            "/telegram",
            telegram_webhook,
            methods=["POST"]
        ),
    ]

    web_app = Starlette(
        routes=routes
    )

    # -----------------------------------------------------
    # Uvicorn
    # -----------------------------------------------------

    config = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )

    server = uvicorn.Server(
        config
    )

    # -----------------------------------------------------
    # Start
    # -----------------------------------------------------

    async with application:

        await application.start()

        # -------------------------------------------------
        # Set Telegram webhook
        # -------------------------------------------------

        if WEBHOOK_SECRET:

            await application.bot.set_webhook(
                url=webhook_url,
                secret_token=WEBHOOK_SECRET,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )

        else:

            await application.bot.set_webhook(
                url=webhook_url,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )

        logger.info(
            "LEX IBAN Bot started."
        )

        logger.info(
            "Webhook: %s",
            webhook_url
        )

        logger.info(
            "Delete delay: %s seconds",
            DELETE_AFTER
        )

        logger.info(
            "Health: %s/health",
            RENDER_EXTERNAL_URL.rstrip("/")
        )

        try:

            await server.serve()

        finally:

            try:

                await application.bot.delete_webhook()

            except Exception:

                logger.exception(
                    "Failed to delete webhook."
                )

            await application.stop()


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    asyncio.run(
        run_server()
            ) 
