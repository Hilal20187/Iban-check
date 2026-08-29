import os
import re
import asyncio
import logging
from html import escape
from http import HTTPStatus

import aiohttp
from bs4 import BeautifulSoup

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

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    ""
).rstrip("/")

WEBHOOK_SECRET = os.getenv(
    "TELEGRAM_WEBHOOK_SECRET",
    ""
)

# Render provides PORT automatically.
PORT = int(
    os.getenv("PORT", "10000")
)

BASE_URL = (
    "https://www.ibancalculator.com/validate/"
)

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

# =========================================================
# GLOBALS
# =========================================================

telegram_app = None
http_server = None


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

    # Also check individual words
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


def iban_checksum_valid(
    iban: str
) -> bool:

    try:

        rearranged = (
            iban[4:]
            + iban[:4]
        )

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

def get_clean_soup(
    html: str
):

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


def get_label_value(
    soup,
    labels
):

    labels_lower = {
        item.lower().strip()
        for item in labels
    }

    # Table extraction
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

    # Definition list
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

    # Strong / bold labels
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

            if ":" in text:

                value = clean_value(
                    text.split(
                        ":",
                        1
                    )[1]
                )

                if not is_bad_value(value):
                    return value

    # Exact label:value extraction
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
# BANK DATA
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
        [
            "p",
            "div",
            "td",
            "li"
        ]
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

        if re.search(
            p
            + r"\s+(?:is\s+)?supported\b",
            text,
            re.IGNORECASE
        ):
            return True

        if re.search(
            p
            + r"\s+(?:is\s+)?not\s+supported\b",
            text,
            re.IGNORECASE
        ):
            return False

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
# IBAN WEBSITE REQUEST
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

        timeout = aiohttp.ClientTimeout(
            total=30
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
        return "Supported"

    if value is False:
        return "Not supported"

    return "Unknown"


def format_result(data):

    iban = escape(
        data.get("iban", "")
    )

    if data.get("error"):

        return (
            "📋 <b>IBAN Check Result</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔢 <code>{iban}</code>\n\n"
            "⚠️ Unable to get the result.\n"
            f"• Reason: "
            f"{escape(data['error'])}\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "By LEX"
        )

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

    bank = escape(
        data.get("bank")
        or "Not available"
    )

    bic = escape(
        data.get("bic")
        or "Not available"
    )

    branch = escape(
        data.get("branch")
        or "Not available"
    )

    address = (
        data.get("address")
        or "Not available"
    )

    address = escape(
        address
    ).replace(
        "\n",
        "<br>"
    )

    country = escape(
        data.get("country")
        or "Not available"
    )

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
            "❌ No valid IBAN found."
        )

        return

    if len(ibans) > 10:

        await update.message.reply_text(
            "❌ Maximum 10 IBANs per message."
        )

        return

    wait_message = (
        await update.message.reply_text(
            f"⏳ Checking {len(ibans)} IBAN(s)..."
        )
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

            # Small delay to avoid
            # hammering the source.
            await asyncio.sleep(1)

    final_text = (
        "\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    ).join(results)

    if len(final_text) > 4000:

        final_text = (
            final_text[:3900]
            + "\n\n⚠️ Results truncated."
        )

    try:

        await wait_message.edit_text(
            final_text,
            parse_mode="HTML"
        )

    except Exception:

        await update.message.reply_text(
            final_text,
            parse_mode="HTML"
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
# TELEGRAM WEBHOOK
# =========================================================

async def telegram_webhook(
    request: Request
):

    global telegram_app

    # Make sure Telegram application
    # is initialized.
    if telegram_app is None:

        logger.error(
            "Telegram application is not ready."
        )

        return PlainTextResponse(
            "Service Unavailable",
            status_code=HTTPStatus.SERVICE_UNAVAILABLE
        )

    # Verify Telegram secret token
    if WEBHOOK_SECRET:

        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            ""
        )

        if received_secret != WEBHOOK_SECRET:

            logger.warning(
                "Invalid Telegram webhook secret."
            )

            return PlainTextResponse(
                "Unauthorized",
                status_code=HTTPStatus.UNAUTHORIZED
            )

    try:

        data = await request.json()

        update = Update.de_json(
            data=data,
            bot=telegram_app.bot
        )

        await telegram_app.update_queue.put(
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
# STARLETTE APP
# =========================================================

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


# =========================================================
# STARTUP
# =========================================================

@web_app.on_event("startup")
async def startup():

    global telegram_app

    logger.info(
        "Starting LEX IBAN Bot..."
    )

    # Validate environment
    if not TOKEN:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set."
        )

    if not RENDER_EXTERNAL_URL:

        raise RuntimeError(
            "RENDER_EXTERNAL_URL is not set."
        )

    webhook_url = (
        RENDER_EXTERNAL_URL
        + "/telegram"
    )

    logger.info(
        "Render URL: %s",
        RENDER_EXTERNAL_URL
    )

    logger.info(
        "Webhook URL: %s",
        webhook_url
    )

    # Create Telegram application
    telegram_app = (
        Application.builder()
        .token(TOKEN)
        .updater(None)
        .build()
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message
        )
    )

    # Initialize Telegram application
    await telegram_app.initialize()

    # Start Telegram application
    await telegram_app.start()

    # Configure webhook
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

    await telegram_app.bot.set_webhook(
        **webhook_kwargs
    )

    logger.info(
        "Telegram webhook configured."
    )

    logger.info(
        "LEX IBAN Bot is ready."
    )


# =========================================================
# SHUTDOWN
# =========================================================

@web_app.on_event("shutdown")
async def shutdown():

    global telegram_app

    logger.info(
        "Graceful shutdown started..."
    )

    if telegram_app is None:

        logger.info(
            "Telegram application already stopped."
        )

        return

    try:

        # Remove webhook cleanly
        await telegram_app.bot.delete_webhook(
            drop_pending_updates=False
        )

        logger.info(
            "Telegram webhook removed."
        )

    except Exception as error:

        logger.exception(
            "Failed to delete Telegram webhook: %s",
            error
        )

    try:

        # Stop receiving/processing updates
        await telegram_app.stop()

        logger.info(
            "Telegram application stopped."
        )

    except Exception as error:

        logger.exception(
            "Telegram application stop error: %s",
            error
        )

    try:

        await telegram_app.shutdown()

        logger.info(
            "Telegram application shutdown complete."
        )

    except Exception as error:

        logger.exception(
            "Telegram application shutdown error: %s",
            error
        )

    telegram_app = None

    logger.info(
        "Graceful shutdown completed."
    )


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    import uvicorn

    logger.info(
        "Starting Uvicorn on port %s",
        PORT
    )

    uvicorn.run(
        web_app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
        ) 
