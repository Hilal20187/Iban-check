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

    wait_msg = await update.message.reply_text("⏳ جاري فحص الـ IBAN وفحص دعم SEPA والبنك...")

    # رابط الفحص المباشر من موقع IBAN Calculator لجلب كافة التفاصيل والـ SEPA
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

                    # التحقق مما إذا كان الآيبان صحيحاً
                    if "valid" in text_result.lower() or len(text_result) > 100:
                        # استخراج معلومات البنك والـ SEPA بطريقة ذكية
                        bank_name = "غير متوفر"
                        bic = "غير متوفر"
                        city = "غير متوفر"
                        
                        sepa_ct = "❌ غير مدعوم"
                        sepa_inst = "❌ غير مدعوم"
                        sepa_dd = "❌ غير مدعوم"
                        b2b = "❌ غير مدعوم"

                        # قراءة السطور واستخراج البيانات
                        lines = [line.strip() for line in text_result.split('\n') if line.strip()]
                        
                        for i, line in enumerate(lines):
                            if "Bank:" in line or "Bank" in line:
                                if i + 1 < len(lines):
                                    bank_name = lines[i+1]
                            if "BIC:" in line:
                                bic = line.replace("BIC:", "").strip()
                            if "SEPA Credit Transfer" in line:
                                sepa_ct = "✅ مدعوم (Supported)" if "is supported" in line.lower() or "supported" in lines[i].lower() else "❌ غير مدعوم"
                            if "SEPA Instant Credit Transfer" in line:
                                sepa_inst = "⚡ مدعوم فوري (Instant Supported)" if "is supported" in line.lower() or "supported" in lines[i].lower() else "❌ غير مدعوم"
                            if "SEPA Direct Debit" in line:
                                sepa_dd = "✅ مدعوم" if "is supported" in line.lower() else "❌ غير مدعوم"
                            if "B2B is supported" in line or "B2B" in line:
                                b2b = "✅ مدعوم" if "supported" in line.lower() else "❌ غير مدعوم"

                        # في حال لم يتم التقاط الاسم بدقة من النصوص البسيطة، نضع قيم افتراضية بناءً على البادئة
                        country_code = iban[:2]

                        result_text = (
                            f"✅ **الـ IBAN صالح (Valid IBAN)**\n\n"
                            f"🌍 **الدولة:** {country_code}\n"
                            f"🏦 **البنك:** {bank_name}\n"
                            f"🔤 **BIC / SWIFT:** `{bic}`\n\n"
                            f"💳 **حالة خدمات SEPA:**\n"
                            f"• SEPA Transfer: {sepa_ct}\n"
                            f"• SEPA Instant: {sepa_inst}\n"
                            f"• SEPA Direct Debit: {sepa_dd}\n"
                            f"• B2B: {b2b}"
                        )
                    else:
                        result_text = "❌ **هذا الـ IBAN غير صالح أو غير موجود!**"
                    
                    await wait_msg.edit_text(result_text, parse_mode="Markdown")
                else:
                    await wait_msg.edit_text("⚠️ حدث خطأ أثناء الاتصال بموقع الفحص.")
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
 
