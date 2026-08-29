import asyncio
import logging
import os
import re
from html import escape
from http import HTTPStatus

import aiohttp
import uvicorn
from bs4 import BeautifulSoup
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

# =========================================================
# CONFIGURATION
# =========================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "",
).strip()

WEBHOOK_SECRET = os.getenv(
    "TELEGRAM_WEBHOOK_SECRET",
    "",
).strip()

BASE_URL = "https://www.ibancalculator.com/validate/"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("LEX-IBAN-BOT")

# =========================================================
# GLOBAL STATE
# =========================================================

application = None

# =========================================================
# IBAN UTILITIES
# =========================================================


def clean_iban(value: str) -> str:
    """Remove formatting characters and normalize an IBAN."""
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def extract_ibans(text: str) -> list[str]:
    """Extract unique, structurally valid IBANs from text."""
    found = []

    pattern = r"\b[A-Z]{2}\s*[0-9]{2}(?:[\sA-Z0-9]{10,40})\b"

    for match in re.findall(pattern, text.upper()):
        iban = clean_iban(match)

        if (
            15 <= len(iban) <= 34
            and re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]+", iban)
            and iban not in found
        ):
            found.append(iban)

    for word in text.split():
        iban = clean_iban(word)

        if (
            15 <= len(iban) <= 34
            and re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]+", iban)
            and iban not in found
        ):
            found.append(iban)

    return found


def iban_checksum_valid(iban: str) -> bool:
    """Validate an IBAN using the ISO 13616 checksum algorithm."""
    try:
        rearranged = iban[4:] + iban[:4]
        numeric = ""

        for character in rearranged:
            if character.isdigit():
                numeric += character
            elif character.isalpha():
                numeric += str(ord(character) - 55)
            else:
                return False

        remainder = 0

        for index in range(0, len(numeric), 7):
            remainder = int(
                str(remainder) + numeric[index : index + 7]
            ) % 97

        return remainder == 1

    except Exception:
        return False


# =========================================================
# HTML PARSING
# =========================================================


def get_clean_soup(html: str) -> BeautifulSoup:
    """Create a BeautifulSoup document without non-content elements."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(["script", "style", "noscript", "svg"]):
        tag.decompose()

    return soup


def clean_value(value: str | None) -> str | None:
    """Normalize extracted text and remove surrounding punctuation."""
    if not value:
        return None

    value = re.sub(r"\s+", " ", value).strip()
    value = value.strip(" \t\r\n:|-")

    return value or None


def is_bad_value(value: str | None) -> bool:
    """Determine whether an extracted value is unusable."""
    if not value:
        return True

    normalized = value.lower().strip()

    invalid_values = {
        "",
        "-",
        "---",
        "not found",
        "not available",
        "unknown",
        "nicht verfügbar",
        "nicht gefunden",
    }

    if normalized in invalid_values:
        return True

    invalid_fragments = [
        "bankleitzahl",
        "bank code",
        "branch code",
        "sort code",
        "clearing code",
        "bank identifier",
    ]

    return any(fragment in normalized for fragment in invalid_fragments)


def get_label_value(soup, labels: list[str]) -> str | None:
    """Extract a value associated with one of the supplied labels."""
    labels_lower = {label.lower().strip() for label in labels}

    # Table extraction
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])

        if len(cells) < 2:
            continue

        label = clean_value(cells[0].get_text(" ", strip=True))

        if not label:
            continue

        if label.lower().rstrip(":") in labels_lower:
            value = clean_value(cells[1].get_text(" ", strip=True))

            if not is_bad_value(value):
                return value

    # Definition list extraction
    for element in soup.find_all(["label", "dt"]):
        label = clean_value(element.get_text(" ", strip=True))

        if not label or label.lower().rstrip(":") not in labels_lower:
            continue

        sibling = element.find_next_sibling()

        if sibling:
            value = clean_value(sibling.get_text(" ", strip=True))

            if not is_bad_value(value):
                return value

    # Strong and bold label extraction
    for element in soup.find_all(["strong", "b"]):
        label = clean_value(element.get_text(" ", strip=True))

        if not label or label.lower().rstrip(":") not in labels_lower:
            continue

        parent = element.parent

        if parent:
            text = clean_value(parent.get_text(" ", strip=True))

            if text and ":" in text:
                value = clean_value(text.split(":", 1)[1])

                if not is_bad_value(value):
                    return value

    # Exact label:value extraction
    for element in soup.find_all(["p", "div", "li", "span"]):
        text = clean_value(element.get_text(" ", strip=True))

        if not text:
            continue

        for label in labels:
            pattern = rf"^{re.escape(label)}\s*:\s*(.+)$"
            match = re.match(pattern, text, re.IGNORECASE)

            if match:
                value = clean_value(match.group(1))

                if not is_bad_value(value):
                    return value

    return None


# =========================================================
# BANK DATA EXTRACTION
# =========================================================


def extract_bank(soup) -> str | None:
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
        ],
    )

    return None if is_bad_value(value) else value


def extract_bic(soup) -> str | None:
    value = get_label_value(soup, ["BIC", "BIC/SWIFT", "SWIFT"])

    if not value:
        return None

    match = re.search(r"\b[A-Z0-9]{8}(?:[A-Z0-9]{3})?\b", value.upper())

    if not match:
        return None

    bic = match.group(0)

    return bic if re.search(r"[A-Z]", bic) else None


def extract_branch(soup) -> str | None:
    value = get_label_value(
        soup,
        [
            "Branch number",
            "Branch Number",
            "Branch",
        ],
    )

    return None if is_bad_value(value) else value


def extract_address(soup) -> str | None:
    value = get_label_value(
        soup,
        [
            "Address",
            "Bank address",
            "Bank Address",
        ],
    )

    if not is_bad_value(value):
        return value

    bank = extract_bank(soup)

    if not bank:
        return None

    elements = soup.find_all(["p", "div", "td", "li"])

    for index, element in enumerate(elements):
        current = clean_value(element.get_text(" ", strip=True))

        if current != bank:
            continue

        possible_values = []

        for next_element in elements[index + 1 : index + 5]:
            text = clean_value(next_element.get_text(" ", strip=True))

            if not text:
                continue

            normalized = text.lower()

            if any(
                marker in normalized
                for marker in [
                    "sepa credit transfer",
                    "sepa direct debit",
                    "sepa instant",
                    "b2b",
                    "branch number",
                ]
            ):
                break

            if text != bank:
                possible_values.append(text)

        if possible_values:
            return "\n".join(possible_values[:3])

    return None


# =========================================================
# SEPA SUPPORT DETECTION
# =========================================================


def detect_support(soup, phrases: list[str]) -> bool | None:
    """Detect whether specified SEPA services are supported."""
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).lower()

    for phrase in phrases:
        escaped_phrase = re.escape(phrase.lower())

        if re.search(
            escaped_phrase + r"\s+(?:is\s+)?supported\b",
            text,
            re.IGNORECASE,
        ):
            return True

        if re.search(
            escaped_phrase + r"\s+(?:is\s+)?not\s+supported\b",
            text,
            re.IGNORECASE,
        ):
            return False

        if re.search(
            escaped_phrase + r".{0,40}(nicht unterstützt|nicht unterstuetzt)",
            text,
            re.IGNORECASE,
        ):
            return False

        if re.search(
            escaped_phrase + r".{0,40}(unterstützt|unterstuetzt)",
            text,
            re.IGNORECASE,
        ):
            return True

    return None


# =========================================================
# PAGE PARSING
# =========================================================


def parse_result(html: str, iban: str) -> dict:
    """Parse validation and bank information from the source page."""
    soup = get_clean_soup(html)
    text = soup.get_text("\n", strip=True)
    normalized_text = text.lower()

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

    if any(
        phrase in normalized_text
        for phrase in [
            "this is a valid iban",
            "this iban is valid",
            "is a valid iban",
            "dies ist eine gültige iban",
        ]
    ):
        result["valid"] = True
    elif any(
        phrase in normalized_text
        for phrase in [
            "this is not a valid iban",
            "this iban is invalid",
            "this is an invalid iban",
            "dies ist keine gültige iban",
        ]
    ):
        result["valid"] = False

    result["bank"] = extract_bank(soup)
    result["bic"] = extract_bic(soup)
    result["branch"] = extract_branch(soup)
    result["address"] = extract_address(soup)
    result["sepa"] = detect_support(soup, ["SEPA Credit Transfer"])
    result["direct_debit"] = detect_support(soup, ["SEPA Direct Debit"])
    result["b2b"] = detect_support(soup, ["B2B"])
    result["instant"] = detect_support(
        soup,
        ["SEPA Instant Credit Transfer"],
    )

    return result


# =========================================================
# IBAN SOURCE REQUEST
# =========================================================


async def check_iban(session, iban: str) -> dict:
    """Retrieve and parse validation data for an IBAN."""
    url = BASE_URL + iban

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        timeout = aiohttp.ClientTimeout(total=30)

        async with session.get(
            url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
        ) as response:
            if response.status != HTTPStatus.OK:
                return {
                    "iban": iban,
                    "error": f"HTTP {response.status}",
                }

            html = await response.text()
            return parse_result(html, iban)

    except asyncio.TimeoutError:
        return {
            "iban": iban,
            "error": "Request timeout.",
        }

    except Exception as error:
        logger.exception("IBAN request failed: %s", error)

        return {
            "iban": iban,
            "error": "Unable to contact the IBAN source.",
        }


# =========================================================
# RESULT FORMATTING
# =========================================================


def support_text(value: bool | None) -> str:
    if value is True:
        return "Supported"

    if value is False:
        return "Not supported"

    return "Unknown"


def format_result(data: dict) -> str:
    iban = escape(data.get("iban", ""))

    if data.get("error"):
        return (
            "📋 <b>IBAN Check Result</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔢 <code>{iban}</code>\n\n"
            "⚠️ Unable to retrieve the result.\n"
            f"• Reason: {escape(data['error'])}\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "By LEX"
        )

    if data.get("valid") is True:
        status = "Valid"
    elif data.get("valid") is False:
        status = "Invalid"
    else:
        status = (
            "Technically valid"
            if iban_checksum_valid(data["iban"])
            else "Technically invalid"
        )

    bank = escape(data.get("bank") or "Not available")
    bic = escape(data.get("bic") or "Not available")
    branch = escape(data.get("branch") or "Not available")
    country = escape(data.get("country") or "Not available")

    address = escape(data.get("address") or "Not available").replace(
        "\n",
        "<br>",
    )

    return (
        "📋 <b>IBAN Check Result</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔢 <code>{iban}</code>\n\n"
        f"• Status: <b>{status}</b>\n"
        f"• Country: <b>{country}</b>\n\n"
        "🏦 <b>Bank Information</b>\n"
        f"• Bank: {bank}\n"
        f"• BIC/SWIFT: <code>{bic}</code>\n"
        f"• Address: {address}\n"
        f"• Branch: <code>{branch}</code>\n\n"
        "💶 <b>SEPA</b>\n"
        f"• SEPA Credit Transfer: {support_text(data.get('sepa'))}\n"
        f"• SEPA Direct Debit: {support_text(data.get('direct_debit'))}\n"
        f"• B2B: {support_text(data.get('b2b'))}\n"
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
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message:
        return

    text = (update.message.text or "").strip()
    ibans = extract_ibans(text)

    if not ibans:
        await update.message.reply_text("❌ No valid IBAN found.")
        return

    if len(ibans) > 10:
        await update.message.reply_text(
            "❌ Maximum 10 IBANs per message."
        )
        return

    wait_message = await update.message.reply_text(
        f"⏳ Checking {len(ibans)} IBAN(s)..."
    )

    async with aiohttp.ClientSession() as session:
        results = []

        for iban in ibans:
            result = await check_iban(session, iban)
            results.append(format_result(result))
            await asyncio.sleep(1)

    final_text = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n".join(results)

    if len(final_text) > 4000:
        final_text = final_text[:3900] + "\n\n⚠️ Results truncated."

    try:
        await wait_message.edit_text(final_text, parse_mode="HTML")
    except Exception as error:
        logger.warning("editMessageText failed: %s", error)

        try:
            await update.message.reply_text(
                final_text,
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Could not send final result.")


# =========================================================
# WEBHOOK
# =========================================================


async def telegram_webhook(request: Request) -> PlainTextResponse:
    global application

    if application is None:
        return PlainTextResponse(
            "Bot is starting",
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        )

    if WEBHOOK_SECRET:
        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            "",
        )

        if received_secret != WEBHOOK_SECRET:
            logger.warning("Invalid webhook secret.")

            return PlainTextResponse(
                "Unauthorized",
                status_code=HTTPStatus.UNAUTHORIZED,
            )

    try:
        data = await request.json()
        update = Update.de_json(data=data, bot=application.bot)

        await application.update_queue.put(update)

        return PlainTextResponse("OK", status_code=HTTPStatus.OK)

    except Exception as error:
        logger.exception("Webhook error: %s", error)

        return PlainTextResponse(
            "Bad Request",
            status_code=HTTPStatus.BAD_REQUEST,
        )


# =========================================================
# HEALTH CHECK
# =========================================================


async def health_check(request: Request) -> PlainTextResponse:
    return PlainTextResponse(
        "LEX IBAN Bot is running.",
        status_code=HTTPStatus.OK,
    )


async def root(request: Request) -> PlainTextResponse:
    return PlainTextResponse(
        "LEX IBAN Bot",
        status_code=HTTPStatus.OK,
    )


# =========================================================
# STARLETTE APPLICATION
# =========================================================

web_app = Starlette(
    routes=[
        Route("/", root, methods=["GET"]),
        Route("/health", health_check, methods=["GET"]),
        Route("/telegram", telegram_webhook, methods=["POST"]),
    ],
)

# =========================================================
# SERVER STARTUP
# =========================================================


async def run_server() -> None:
    global application

    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    if not RENDER_EXTERNAL_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL is not set.")

    webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/telegram"

    logger.info("Starting LEX IBAN Bot...")
    logger.info("Render URL: %s", RENDER_EXTERNAL_URL)
    logger.info("Webhook URL: %s", webhook_url)

    application = (
        Application.builder()
        .token(TOKEN)
        .updater(None)
        .build()
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    await application.initialize()
    logger.info("Telegram application initialized.")

    await application.start()
    logger.info("Telegram application started.")

    webhook_kwargs = {
        "url": webhook_url,
        "allowed_updates": Update.ALL_TYPES,
        "drop_pending_updates": True,
    }

    if WEBHOOK_SECRET:
        webhook_kwargs["secret_token"] = WEBHOOK_SECRET

    try:
        await application.bot.set_webhook(**webhook_kwargs)
        logger.info("Telegram webhook configured successfully.")

        webhook_info = await application.bot.get_webhook_info()
        logger.info("Telegram webhook active: %s", webhook_info.url)

        if webhook_info.last_error_message:
            logger.warning(
                "Telegram webhook last error: %s",
                webhook_info.last_error_message,
            )

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

        server = uvicorn.Server(config)

        logger.info("LEX IBAN Bot is ready.")
        logger.info(
            "Health endpoint: %s/health",
            RENDER_EXTERNAL_URL.rstrip("/"),
        )

        await server.serve()

    finally:
        logger.info("Graceful shutdown started...")

        if application:
            try:
                await application.bot.delete_webhook(
                    drop_pending_updates=False,
                )
                logger.info("Telegram webhook deleted.")
            except Exception as error:
                logger.warning("Could not delete webhook: %s", error)

            try:
                if application.running:
                    await application.stop()
                    logger.info("Telegram application stopped.")
            except Exception as error:
                logger.warning(
                    "Telegram application stop failed: %s",
                    error,
                )

            try:
                await application.shutdown()
                logger.info(
                    "Telegram application shutdown complete."
                )
            except Exception as error:
                logger.warning(
                    "Telegram application shutdown failed: %s",
                    error,
                )

        application = None
        logger.info("Graceful shutdown completed.")


# =========================================================
# MAIN ENTRY POINT
# =========================================================

if __name__ == "__main__":
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        logger.info("Process interrupted.")
    except Exception:
        logger.exception("Fatal application error.")
        raise 
