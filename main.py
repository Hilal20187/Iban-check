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
    iban = update.message.text.strip().replace(" ", "").upper()
    
    if len(iban) < 15:
        await update.message.reply_text("❌ هذا النص قصير جداً ليكون رقم IBAN صالح.")
        return

    wait_msg = await update.message.reply_text("⏳ جاري فحص الـ IBAN وجلب تفاصيل البنك...")

    api_url = f"https://openiban.com/validate/{iban}?getBIC=true"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(api_url) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    if data.get("valid"):
                        bank_data = data.get("bankData") or {}
                        bank_name = bank_data.get("name") or "غير متوفر"
                        bic = bank_data.get("bic") or "غير متوفر"
                        city = bank_data.get("city") or "غير متوفر"
                        country = data.get("country") or iban[:2]
                        
                        account_data = data.get("accountData") or {}
                        account_num = account_data.get("account") or "غير متوفر"
                        
                        result_text = (
                            f"✅ **الـ IBAN صالح (Valid)**\n\n"
                            f"🏦 **البنك:** {bank_name}\n"
                            f"🔤 **BIC / SWIFT:** `{bic}`\n"
                            f"📍 **المدينة:** {city}\n"
                            f"🌍 **الدولة:** {country}\n"
                            f"🔢 **رقم الحساب:** `{account_num}`"
                        )
                    else:
                        result_text = "❌ **هذا الـ IBAN غير صالح أو وهمي!**"
                    
                    await wait_msg.edit_text(result_text, parse_mode="Markdown")
                else:
                    await wait_msg.edit_text("⚠️ حدث خطأ في الاتصال بخدمة الفحص.")
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
