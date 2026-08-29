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

PORT = int(
    os.getenv("PORT", "10000")
)

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    ""
).strip()

WEBHOOK_SECRET = os.getenv(
    "TELEGRAM_WEBHOOK_SECRET",
    ""
).strip()

BASE_URL = (
    "https://www.ibancalculator.com/validate/"
)

DELETE_AFTER_SECONDS = 10

logging.basicConfig(
    format=(
        "%(asctime)s - %(name)s - "
        "%(levelname)s - %(message)s"
    ),
    level=logging.INFO,
)

logger = logging.getLogger(
    "LEX-IBAN-BOT"
)

application = None


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
# HTML PARSING
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
        item in low
        for item in bad_fragments
    ):

        return True

    return False


# =========================================================
# LABEL / VALUE EXTRACTION
# =========================================================

def get_label_value(
    soup,
    labels
):

    labels_lower = {
        item.lower().strip()
        for item in labels
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

        if (
            label.lower().rstrip(":")
            in labels_lower
        ):

            value = clean_value(
                cells[1].get_text(
                    " ",
                    strip=True
                )
            )

            if not is_bad_value(value):

                return value

    # -----------------------------------------------------
    # DEFINITION LIST
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

        if (
            label.lower().rstrip(":")
            not in labels_lower
        ):

            continue

        sibling = (
            element.find_next_sibling()
        )

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
    # STRONG / BOLD
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

        if (
            label.lower().rstrip(":")
            not in labels_lower
        ):

            continue

        parent = element.parent

        if parent:

            text = clean_value(
                parent.get_text(
                    " ",
                    strip=True
                )
            )

            if text and ":" in text:

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
        [
            "p",
            "div",
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
# BIC / SWIFT
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

    # -----------------------------------------------------
    # 1. DIRECT ADDRESS FIELD
    # -----------------------------------------------------

    value = get_label_value(
        soup,
        [
            "Address",
            "Bank address",
            "Bank Address",
            "Adresse",
            "Bankadresse",
            "Adresse der Bank",
        ]
    )

    if not is_bad_value(value):

        return value

    # -----------------------------------------------------
    # 2. SEARCH AFTER BANK NAME
    # -----------------------------------------------------

    bank = extract_bank(
        soup
    )

    if not bank:

        return None

    elements = soup.find_all(
        [
            "p",
            "div",
            "td",
            "li"
        ]
    )

    for i, element in enumerate(
        elements
    ):

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
            i + 1:i + 8
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
                or "bic"
                in low
                or "swift"
                in low
            ):

                break

            if text == bank:

                continue

            if len(text) > 180:

                continue

            possible.append(text)

        if possible:

            return "\n".join(
                possible[:3]
            )

    return None


# =========================================================
# ADDRESS FALLBACK
# =========================================================

def extract_address_fallback(soup):

    text = soup.get_text(
        "\n",
        strip=True
    )

    lines = []

    for line in text.splitlines():

        line = line.strip()

        if not line:

            continue

        low = line.lower()

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
            or "bic"
            in low
            or "swift"
            in low
        ):

            continue

        # Common address indicators
        if re.search(
            r"\b\d{1,5}\s+",
            line
        ):

            if len(line) <= 180:

                lines.append(line)

    if lines:

        return "\n".join(
            lines[:3]
        )

    return None


def get_address(soup):

    address = extract_address(
        soup
    )

    if address:

        return address

    return extract_address_fallback(
        soup
    )


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

        # English positive
        if re.search(
            p
            + r"\s+(?:is\s+)?supported\b",
            text,
            re.IGNORECASE
        ):

            return True

        # English negative
        if re.search(
            p
            + r"\s+(?:is\s+)?not\s+supported\b",
            text,
            re.IGNORECASE
        ):

            return False

        # German negative
        if re.search(
            p
            + r".{0,40}"
            + r"(nicht unterstützt|nicht unterstuetzt)",
            text,
            re.IGNORECASE
        ):

            return False

        # German positive
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
# PAGE PARSER
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
        "this is a valid iban"
        in lower
        or "this iban is valid"
        in lower
        or "is a valid iban"
        in lower
        or "dies ist eine gültige iban"
        in lower
    ):

        result["valid"] = True

    elif (
        "this is not a valid iban"
        in lower
        or "this iban is invalid"
        in lower
        or "this is an invalid iban"
        in lower
        or "dies ist keine gültige iban"
        in lower
    ):

        result["valid"] = False

    # -----------------------------------------------------
    # BANK DATA
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

    result["address"] = get_address(
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
# IBAN REQUEST
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
        "Accept-Language": (
            "en-US,en;q=0.9"
        ),
        "Accept": (
            "text/html,"
            "application/xhtml+xml,"
            "application/xml;q=0.9,"
            "*/*;q=0.8"
        ),
    }

    try:

        timeout = aiohttp.ClientTimeout(
            total=30,
            connect=10,
            sock_read=25
        )

        async with session.get(
            url,
            headers=headers,
            timeout=timeout,
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

    return "Unknown"


def format_result(data):

    iban = escape(
        data.get("iban", "")
    )

    if data.get("error"):

        return (
            "IBAN check By transfer:\n"
            "📋 <b>IBAN Check Result</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"

            f"🔢 <code>{iban}</code>\n\n"

            "⚠️ Unable to get the result.\n"
            f"• Reason: "
            f"{escape(data['error'])}\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "By LEX"
        )

    # -----------------------------------------------------
    # STATUS
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
    # BANK
    # -----------------------------------------------------

    bank = escape(
        data.get("bank")
        or "Not available"
    )

    # -----------------------------------------------------
    # BIC
    # -----------------------------------------------------

    bic = escape(
        data.get("bic")
        or "Not available"
    )

    # -----------------------------------------------------
    # ADDRESS
    # -----------------------------------------------------

    # IMPORTANT:
    # Do NOT convert \n to <br>.
    # Telegram HTML does not support <br>.
    address = (
        data.get("address")
        or "Not available"
    )

    address = escape(
        address
    )

    # -----------------------------------------------------
    # BRANCH
    # -----------------------------------------------------

    branch = escape(
        data.get("branch")
        or "Not available"
    )

    # -----------------------------------------------------
    # COUNTRY
    # -----------------------------------------------------

    country = escape(
        data.get("country")
        or "Not available"
    )

    # -----------------------------------------------------
    # FINAL MESSAGE
    # -----------------------------------------------------

    return (
        "IBAN check By transfer:\n"
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
    user_message,
    result_message
):

    try:

        await asyncio.sleep(
            DELETE_AFTER_SECONDS
        )

        # -------------------------------------------------
        # DELETE USER IBAN MESSAGE
        # -------------------------------------------------

        try:

            await user_message.delete()

            logger.info(
                "Deleted message %s from chat %s",
                user_message.message_id,
                user_message.chat_id
            )

        except Exception as error:

            logger.warning(
                "Could not delete user message "
                "%s: %s",
                user_message.message_id,
                error
            )

        # -------------------------------------------------
        # DELETE BOT RESULT
        # -------------------------------------------------

        try:

            await result_message.delete()

            logger.info(
                "Deleted message %s from chat %s",
                result_message.message_id,
                result_message.chat_id
            )

        except Exception as error:

            logger.warning(
                "Could not delete bot result "
                "%s: %s",
                result_message.message_id,
                error
            )

    except asyncio.CancelledError:

        logger.info(
            "Delete task cancelled during shutdown."
        )

        raise


# =========================================================
# TELEGRAM MESSAGE HANDLER
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
    # EXTRACT IBANS
    # -----------------------------------------------------

    ibans = extract_ibans(
        text
    )

    # -----------------------------------------------------
    # NO IBAN
    # -----------------------------------------------------

    if not ibans:

        try:

            await update.message.reply_text(
                "❌ No valid IBAN found."
            )

        except Exception:

            logger.exception(
                "Failed to send no-IBAN message."
            )

        return

    # -----------------------------------------------------
    # MAXIMUM
    # -----------------------------------------------------

    if len(ibans) > 10:

        try:

            await update.message.reply_text(
                "❌ Maximum 10 IBANs per message."
            )

        except Exception:

            logger.exception(
                "Failed to send maximum message."
            )

        return

    # Save original user message
    user_message = update.message

    # -----------------------------------------------------
    # WAIT MESSAGE
    # -----------------------------------------------------

    try:

        wait_message = (
            await update.message.reply_text(
                f"⏳ Checking "
                f"{len(ibans)} IBAN(s)..."
            )
        )

    except Exception:

        logger.exception(
            "Failed to send waiting message."
        )

        return

    # -----------------------------------------------------
    # REQUEST SESSION
    # -----------------------------------------------------

    connector = aiohttp.TCPConnector(
        limit=5,
        ttl_dns_cache=300
    )

    timeout = aiohttp.ClientTimeout(
        total=35
    )

    try:

        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout
        ) as session:

            results = []

            for iban in ibans:

                result = await check_iban(
                    session,
                    iban
                )

                results.append(
                    format_result(result)
                )

                # Avoid hammering source
                await asyncio.sleep(1)

    except Exception as error:

        logger.exception(
            "Processing failed: %s",
            error
        )

        try:

            await wait_message.edit_text(
                "⚠️ An error occurred while "
                "checking the IBAN.",
                parse_mode="HTML"
            )

        except Exception:

            pass

        return

    # -----------------------------------------------------
    # COMBINE RESULTS
    # -----------------------------------------------------

    final_text = (
        "\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    ).join(results)

    # Telegram HTML message limit
    if len(final_text) > 4000:

        final_text = (
            final_text[:3900]
            + "\n\n⚠️ Results truncated."
        )

    # -----------------------------------------------------
    # EDIT WAIT MESSAGE
    # -----------------------------------------------------

    try:

        await wait_message.edit_text(
            final_text,
            parse_mode="HTML"
        )

        result_message = wait_message

    except Exception as error:

        logger.warning(
            "editMessageText failed: %s",
            error
        )

        try:

            result_message = (
                await update.message.reply_text(
                    final_text,
                    parse_mode="HTML"
                )
            )

            try:

                await wait_message.delete()

            except Exception:

                pass

        except Exception:

            logger.exception(
                "Could not send final result."
            )

            return

    # -----------------------------------------------------
    # DELETE AFTER 10 SECONDS
    # -----------------------------------------------------

    asyncio.create_task(
        delete_messages_after_delay(
            user_message,
            result_message
        )
    )


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

async def telegram_webhook(
    request: Request
):

    global application

    if application is None:

        return PlainTextResponse(
            "Bot is starting",
            status_code=(
                HTTPStatus.SERVICE_UNAVAILABLE
            )
        )

    # -----------------------------------------------------
    # SECRET
    # -----------------------------------------------------

    if WEBHOOK_SECRET:

        received_secret = (
            request.headers.get(
                "X-Telegram-Bot-Api-Secret-Token",
                ""
            )
        )

        if received_secret != WEBHOOK_SECRET:

            logger.warning(
                "Invalid Telegram webhook secret."
            )

            return PlainTextResponse(
                "Unauthorized",
                status_code=(
                    HTTPStatus.UNAUTHORIZED
                )
            )

    # -----------------------------------------------------
    # JSON
    # -----------------------------------------------------

    try:

        data = await request.json()

    except Exception:

        return PlainTextResponse(
            "Invalid JSON",
            status_code=(
                HTTPStatus.BAD_REQUEST
            )
        )

    # -----------------------------------------------------
    # UPDATE
    # -----------------------------------------------------

    try:

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
            "Webhook processing error: %s",
            error
        )

        return PlainTextResponse(
            "Bad Request",
            status_code=(
                HTTPStatus.BAD_REQUEST
            )
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
# STARLETTE
# =========================================================

web_app = Starlette(
    routes=[
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
)


# =========================================================
# RUN SERVER
# =========================================================

async def run_server():

    global application

    # -----------------------------------------------------
    # ENVIRONMENT CHECK
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

    logger.info(
        "Starting LEX IBAN Bot..."
    )

    logger.info(
        "Webhook URL: %s",
        webhook_url
    )

    # -----------------------------------------------------
    # TELEGRAM APPLICATION
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
    # INITIALIZE
    # -----------------------------------------------------

    await application.initialize()

    logger.info(
        "Telegram application initialized."
    )

    # -----------------------------------------------------
    # START
    # -----------------------------------------------------

    await application.start()

    logger.info(
        "Telegram application started."
    )

    try:

        # -------------------------------------------------
        # SET WEBHOOK
        # -------------------------------------------------

        webhook_kwargs = {
            "url": webhook_url,
            "allowed_updates": (
                Update.ALL_TYPES
            ),
            "drop_pending_updates": True,
        }

        if WEBHOOK_SECRET:

            webhook_kwargs[
                "secret_token"
            ] = WEBHOOK_SECRET

        await application.bot.set_webhook(
            **webhook_kwargs
        )

        logger.info(
            "Telegram webhook configured."
        )

        # -------------------------------------------------
        # CHECK WEBHOOK
        # -------------------------------------------------

        webhook_info = (
            await application.bot.get_webhook_info()
        )

        logger.info(
            "Active webhook: %s",
            webhook_info.url
        )

        if webhook_info.last_error_message:

            logger.warning(
                "Telegram webhook last error: %s",
                webhook_info.last_error_message
            )

        # -------------------------------------------------
        # UVICORN
        # -------------------------------------------------

        config = uvicorn.Config(
            web_app,
            host="0.0.0.0",
            port=PORT,
            log_level="info",
            access_log=True,
            proxy_headers=True,
            forwarded_allow_ips="*",
            timeout_keep_alive=15,
        )

        server = uvicorn.Server(
            config
        )

        logger.info(
            "LEX IBAN Bot is ready."
        )

        logger.info(
            "Health: %s/health",
            RENDER_EXTERNAL_URL.rstrip("/")
        )

        # -------------------------------------------------
        # RUN
        # -------------------------------------------------

        await server.serve()

    finally:

        # -------------------------------------------------
        # GRACEFUL SHUTDOWN
        # -------------------------------------------------

        logger.info(
            "Graceful shutdown started..."
        )

        if application:

            # ---------------------------------------------
            # DELETE WEBHOOK
            # ---------------------------------------------

            try:

                await application.bot.delete_webhook(
                    drop_pending_updates=False
                )

                logger.info(
                    "Telegram webhook deleted."
                )

            except Exception as error:

                logger.warning(
                    "Could not delete webhook: %s",
                    error
                )

            # ---------------------------------------------
            # STOP
            # ---------------------------------------------

            try:

                if application.running:

                    await application.stop()

                    logger.info(
                        "Telegram application stopped."
                    )

            except Exception as error:

                logger.warning(
                    "Application stop failed: %s",
                    error
                )

            # ---------------------------------------------
            # SHUTDOWN
            # ---------------------------------------------

            try:

                await application.shutdown()

                logger.info(
                    "Telegram application shutdown complete."
                )

            except Exception as error:

                logger.warning(
                    "Application shutdown failed: %s",
                    error
                )

        application = None

        logger.info(
            "Graceful shutdown completed."
        )


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            run_server()
        )

    except KeyboardInterrupt:

        logger.info(
            "Process interrupted."
        )

    except Exception:

        logger.exception(
            "Fatal application error."
        )

        raise 
