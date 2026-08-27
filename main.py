import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
import aiohttp
from bs4 import BeautifulSoup

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iban = update.message.text.strip().replace(" ", "").upper()
    
    if len(iban) < 15:
        await update.message.reply_text("❌ هذا النص قصير جداً ليكون رقم IBAN صالح.")
        return

    wait_msg = await update.message.reply_text("⏳ جاري فحص الـ IBAN وجلب تفاصيل البنك وخدمات SEPA...")

    url = f"https://ibancalculator.com/call.php?aval={iban}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    html_content = await response.text()
                    soup = BeautifulSoup(html_content, 'html.parser')
                    
                    text_result = soup.get_text()

                    if "valid" in text_result.lower():
                        bank_name = "غير متوفر"
                        bic = "غير متوفر"
                        
                        sepa_ct = "❌ غير مدعوم"
                        sepa_inst = "❌ غير مدعوم"
                        sepa_dd = "❌ غير مدعوم"
                        b2b = "❌ غير مدعوم"

                        lines = [line.strip() for line in text_result.split('\n') if line.strip()]
                        
                        for i, line in enumerate(lines):
                            if "Bank:" in line:
                                if i + 1 < len(lines):
                                    bank_name = lines[i+1]
                            elif line.startswith("BIC:") or "BIC:" in line:
                                parts = line.split("BIC:")
                                if len(parts) > 1 and len(parts[1].strip()) > 0:
                                    bic = parts[1].strip()

                        full_text_lower = text_result.lower()
                        
                        if "sepa credit transfer is supported" in full_text_lower:
                            sepa_ct = "✅ مدعوم (Supported)"
                        if "sepa instant credit transfer is supported" in full_text_lower:
                            sepa_inst = "⚡ مدعوم فوري (Instant Supported)"
                        if "sepa direct debit is supported" in full_text_lower:
                            sepa_dd = "✅ مدعوم (Supported)"
                        if "b2b is supported" in full_text_lower:
                            b2b = "✅ مدعوم (Supported)"

                        if bic == "غير متوفر":
                            for line in lines:
                                if len(line) == 11 and line[:4].isalpha():
                                    bic = line
                                    break

                        if iban.startswith("LT") and "32500" in iban:
                            bank_name = "Revolut Bank UAB (Payments)"
                            bic = "REVOLT21XXX" if bic == "غير متوفر" else bic
                        elif iban.startswith("NL") and "FNOM" in iban:
                            bank_name = "FNOM / البنك الهولندي"

                        result_text = (
                            f"✅ **الـ IBAN صالح (Valid IBAN)**\n\n"
                            f"🏦 **البنك:** {bank_name}\n"
                            f"🔤 **BIC / SWIFT:** `{bic}`\n"
                            f"📍 **الدولة:** {iban[:2]}\n\n"
                            f"💳 **حالة خدمات SEPA:**\n"
                            f"• SEPA Transfer: {sepa_ct}\n"
                            f"• SEPA Instant: {sepa_inst}\n"
                            f"• SEPA Direct Debit: {sepa_dd}\n"
                            f"• B2B: {b2b}"
                        )
                    else:
                        result_text = "❌ **هذا الـ IBAN غير صالح أو وهمي!**"
                    
                    await wait_msg.edit_text(result_text, parse_mode="Markdown")
                else:
                    await wait_msg.edit_text("⚠️ تعذر الاتصال بموقع الفحص.")
        except Exception as e:
            await wait_msg.edit_text(f"⚠️ خطأ تقني: {str(e)}")

def main():
    if not TOKEN:
        return

    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    application.run_polling()

if __name__ == "__main__":
    main()
 
