import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
import aiohttp

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iban = update.message.text.strip().replace(" ", "")
    
    # رسالة مؤقتة للانتظار
    wait_msg = await update.message.reply_text("⏳ جاري فحص الـ IBAN وجلب المعلومات...")

    api_url = f"https://openiban.com/validate/{iban}?getBIC=true"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(api_url) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    if data.get("valid"):
                        bank_data = data.get("bankData", {})
                        bank_name = bank_data.get("name", "غير معروف")
                        bic = bank_data.get("bic", "غير متوفر")
                        city = bank_data.get("city", "غير متوفر")
                        zip_code = bank_data.get("zip", "")
                        
                        result_text = (
                            f"✅ **هذا الـ IBAN صحيح (Valid IBAN)**\n\n"
                            f"🏦 **البنك:** {bank_name}\n"
                            f"🔤 **BIC / SWIFT:** `{bic}`\n"
                            f"📍 **المدينة:** {city} {zip_code}\n"
                            f"🌍 **الدولة:** {data.get('country', 'غير معروف')}\n"
                            f"🔢 **رقم الحساب:** `{data.get('accountData', {}).get('account', 'غير متوفر')}`"
                        )
                    else:
                        result_text = "❌ **هذا الـ IBAN غير صالح أو وهمي!** يرجى التأكد من الرقم وإعادة إرساله."
                    
                    await wait_msg.edit_text(result_text, parse_mode="Markdown")
                else:
                    await wait_msg.edit_text("⚠️ حدث خطأ أثناء الاتصال بخدمة فحص الـ IBAN.")
        except Exception as e:
            await wait_msg.edit_text(f"⚠️ حدث خطأ تقني: {str(e)}")

def main():
    if not TOKEN:
        print("خطأ: لم يتم العثور على التوكن!")
        return

    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    print("البوت يعمل الآن وجاهز لفحص الـ IBAN...")
    application.run_polling()

if __name__ == "__main__":
    main()
