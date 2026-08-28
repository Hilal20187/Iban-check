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

BASE_URL = "https://www.ibancalculator.com/validate/"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("iban-bot")


# =========================================================
# IBAN FUNCTIONS
# =========================================================

def clean_iban(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def extract_ibans(text: str):
    found = []

    # البحث عن IBAN مع أو بدون مسافات
    pattern = r"\b[A-Z]{2}\s*[0-9]{2}(?:[\sA-Z0-9]{10,40})\b"

    for match in re.findall(pattern, text.upper()):
        iban = clean_iban(match)

        if (
            15 <= len(iban) <= 34
            and re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]+", iban)
            and iban not in found
        ):
            found.append(iban)

    # fallback
    for word in text.split():
        iban = clean_iban(word)

        if (
            15 <= len(iban) <= 34
            and re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]+", iban)
            and iban not in found
        ):
            found.append(iban)

    return found


# =========================================================
# LOCAL IBAN MOD-97
# =========================================================

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
# HTML HELPERS
# =========================================================

def get_clean_soup(html: str):
    soup = BeautifulSoup(html, "html.parser")

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

    if not value:
        return None

    return value


def is_bad_value(value):
    if not value:
        return True

    value_lower = value.lower().strip()

    bad_values = {
        "",
        "-",
        "---",
        "not found",
        "not available",
        "nicht verfügbar",
        "nicht gefunden",
        "non disponible",
        "non trovato",
        "غير متوفر",
        "unknown",
    }

    if value_lower in bad_values:
        return True

    # مهم جدًا:
    # لا نسمح لنصوص Bankleitzahl / bank code
    # أن تصبح اسم البنك.
    bad_fragments = [
        "bankleitzahl",
        "bank code",
        "branch code",
        "sort code",
        "clearing code",
        "bank identifier",
    ]

    if any(
        fragment in value_lower
        for fragment in bad_fragments
    ):
        return True

    return False


def get_label_value_from_text(
    soup,
    labels,
):
    """
    يبحث عن label واضح داخل عناصر HTML،
    وليس مجرد أول كلمة Bank في الصفحة.
    """

    labels_lower = {
        label.lower().strip()
        for label in labels
    }

    # -----------------------------------------------------
    # الطريقة 1: عناصر table
    # -----------------------------------------------------

    for row in soup.find_all("tr"):

        cells = row.find_all(
            ["th", "td"]
        )

        if len(cells) < 2:
            continue

        first = clean_value(
            cells[0].get_text(
                " ",
                strip=True
            )
        )

        if not first:
            continue

        first_lower = first.lower().rstrip(":")

        if first_lower in labels_lower:

            value = clean_value(
                cells[1].get_text(
                    " ",
                    strip=True
                )
            )

            if not is_bad_value(value):
                return value

    # -----------------------------------------------------
    # الطريقة 2: عناصر label
    # -----------------------------------------------------

    for label in soup.find_all(
        ["label", "dt", "strong", "b"]
    ):

        label_text = clean_value(
            label.get_text(
                " ",
                strip=True
            )
        )

        if not label_text:
            continue

        label_lower = (
            label_text
            .lower()
            .rstrip(":")
        )

        if label_lower not in labels_lower:
            continue

        # العنصر التالي
        next_element = label.find_next()

        if next_element:

            value = clean_value(
                next_element.get_text(
                    " ",
                    strip=True
                )
            )

            if (
                value
                and value.lower()
                != label_text.lower()
                and not is_bad_value(value)
            ):
                return value

    # -----------------------------------------------------
    # الطريقة 3: عناصر تحتوي label:value
    # لكن فقط إذا كان الـlabel مطابقًا بالكامل
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
# EXTRACT BIC SAFELY
# =========================================================

def extract_bic(soup):
    value = get_label_value_from_text(
        soup,
        [
            "BIC",
            "BIC/SWIFT",
            "SWIFT",
        ],
    )

    if not value:
        return None

    # BIC الحقيقي يكون 8 أو 11 حرف/رقم
    match = re.search(
        r"\b[A-Z0-9]{8}(?:[A-Z0-9]{3})?\b",
        value.upper()
    )

    if not match:
        return None

    bic = match.group(0)

    # BIC لا يكون مجرد أرقام
    if not re.search("[A-Z]", bic):
        return None

    return bic


# =========================================================
# EXTRACT BANK
# =========================================================

def extract_bank(soup):
    value = get_label_value_from_text(
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

    if is_bad_value(value):
        return None

    # حماية إضافية ضد Bankleitzahl
    if value:

        lower = value.lower()

        forbidden = [
            "bankleitzahl",
            "bank code",
            "branch code",
            "sort code",
            "clearing code",
        ]

        if any(
            x in lower
            for x in forbidden
        ):
            return None

    return value


# =========================================================
# EXTRACT BRANCH
# =========================================================

def extract_branch(soup):
    value = get_label_value_from_text(
        soup,
        [
            "Branch number",
            "Branch Number",
            "Branch",
            "Branch code",
        ],
    )

    if is_bad_value(value):
        return None

    return value


# =========================================================
# EXTRACT ADDRESS
# =========================================================

def extract_address(soup):
    value = get_label_value_from_text(
        soup,
        [
            "Address",
            "Bank address",
            "Bank Address",
        ],
    )

    if not is_bad_value(value):
        return value

    # -----------------------------------------------------
    # إذا لم يوجد label واضح:
    # نبحث عن كتلة العنوان بعد اسم البنك/BIC
    # -----------------------------------------------------

    bank = extract_bank(soup)

    if not bank:
        return None

    all_elements = soup.find_all(
        ["p", "div", "td", "li"]
    )

    for i, element in enumerate(
        all_elements
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

        for next_element in all_elements[
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
# SUPPORT DETECTION
# =========================================================

def detect_support(
    soup,
    exact_phrases,
):
    """
    لا نعتبر وجود كلمة SEPA وحدها دعمًا.
    نبحث عن جملة واضحة مثل:
    SEPA Credit Transfer is supported.
    """

    full_text = soup.get_text(
        " ",
        strip=True
    )

    normalized = re.sub(
        r"\s+",
        " ",
        full_text
    ).lower()

    for phrase in exact_phrases:

        p = re.escape(
            phrase.lower()
        )

        # supported
        if re.search(
            p
            + r"\s+(?:is\s+)?supported\b",
            normalized,
            re.IGNORECASE,
        ):
            return True

        # not supported
        if re.search(
            p
            + r"\s+(?:is\s+)?not\s+supported\b",
            normalized,
            re.IGNORECASE,
        ):
            return False

        # German
        if re.search(
            p
            + r".{0,30}"
            r"(nicht unterstützt|nicht unterstuetzt)",
            normalized,
            re.IGNORECASE,
        ):
            return False

        if re.search(
            p
            + r".{0,30}"
            r"(unterstützt|unterstuetzt)",
            normalized,
            re.IGNORECASE,
        ):
            return True

    return None


# =========================================================
# PARSE PAGE
# =========================================================

def parse_result(html, iban):

    soup = get_clean_soup(html)

    visible_text = soup.get_text(
        "\n",
        strip=True
    )

    lower_text = visible_text.lower()

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

    # =====================================================
    # VALID
    # =====================================================

    if (
        "this is a valid iban" in lower_text
        or "this iban is valid" in lower_text
        or "is a valid iban" in lower_text
        or "dies ist eine gültige iban" in lower_text
    ):
        result["valid"] = True

    elif (
        "this is not a valid iban" in lower_text
        or "this iban is invalid" in lower_text
        or "this is an invalid iban" in lower_text
        or "dies ist keine gültige iban" in lower_text
    ):
        result["valid"] = False

    # =====================================================
    # BANK INFORMATION
    # =====================================================

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

    # =====================================================
    # SEPA
    # =====================================================

    result["sepa"] = detect_support(
        soup,
        [
            "SEPA Credit Transfer",
        ],
    )

    result["direct_debit"] = detect_support(
        soup,
        [
            "SEPA Direct Debit",
        ],
    )

    result["b2b"] = detect_support(
        soup,
        [
            "B2B",
        ],
    )

    result["instant"] = detect_support(
        soup,
        [
            "SEPA Instant Credit Transfer",
        ],
    )

    return result


# =========================================================
# ONLINE REQUEST
# =========================================================

async def check_online(
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
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
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
                    "iban": iban,
                    "error": (
                        f"HTTP {response.status}"
                    ),
                }

            html = await response.text()

            return parse_result(
                html,
                iban
            )

    except asyncio.TimeoutError:

        return {
            "iban": iban,
            "error": (
                "انتهت مهلة الاتصال بالموقع."
            ),
        }

    except Exception as e:

        logger.exception(
            "Website request failed"
        )

        return {
            "iban": iban,
            "error": (
                "حدث خطأ أثناء الاتصال بالموقع."
            ),
        }


# =========================================================
# FORMAT RESULT
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
            "⚠️ تعذر الحصول على النتيجة من الموقع.\n"
            f"• السبب: {escape(data['error'])}"
        )

    # -----------------------------------------------------
    # STATUS
    # -----------------------------------------------------

    if data.get("valid") is True:

        status = "✅ صالح"

    elif data.get("valid") is False:

        status = "❌ غير صالح"

    else:

        # نستخدم الفحص الرياضي فقط إذا الموقع
        # لم يعط حالة واضحة.
        if iban_checksum_valid(
            data["iban"]
        ):
            status = (
                "⚠️ صالح فنيًا، "
                "لكن المصدر لم يعط حالة واضحة"
            )
        else:
            status = (
                "❌ غير صالح فنيًا"
            )

    # -----------------------------------------------------
    # VALUES
    # -----------------------------------------------------

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
        "ℹ️ المصدر: IBAN Calculator\n"
        "⚠️ صلاحية IBAN لا تعني أن الحساب "
        "مفتوح أو يحتوي على رصيد."
    )


# =========================================================
# TELEGRAM HANDLER
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
            "❌ لم أجد IBAN صالحًا.\n\n"
            "أرسل رقم IBAN واحد أو قائمة IBANs."
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

            result = await check_online(
                session,
                iban
            )

            results.append(
                format_result(
                    result
                )
            )

            # تقليل الضغط على الموقع
            await asyncio.sleep(1)

    final_text = (
        "\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    ).join(results)

    # Telegram limit
    if len(final_text) > 4000:

        final_text = (
            final_text[:3900]
            + "\n\n⚠️ تم اختصار النتائج."
        )

    try:

        await wait.edit_text(
            final_text,
            parse_mode="HTML",
        )

    except Exception:

        await update.message.reply_text(
            final_text,
            parse_mode="HTML",
        )


# =========================================================
# MAIN
# =========================================================

def main():

    if not TOKEN:

        raise RuntimeError(
            "❌ لم يتم العثور على "
            "TELEGRAM_BOT_TOKEN "
            "في Environment Variables."
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
            handle_message,
        )
    )

    logger.info(
        "IBAN Bot started."
    )

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main() 
