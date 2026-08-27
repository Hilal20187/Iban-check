import logging
import aiohttp
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

# إعداد الـ Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# رمز البوت تاعك من BotFather
TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"

async def check_iban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip().replace(" ", "").upper()
    
    # تحقق بسيط إذا النص يبدو كـ IBAN
    if len(user_text) < 15 or not user_text[:2].isalpha():
        return 

    await update.message.reply_text("⏳ جاري التحقق من الـ IBAN...")

    api_url = f"https://openiban.com/validate/{user_text}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(api_url) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    if data.get("valid"):
                        bank_data = data.get("bankData", {})
                        iban_data = data.get("ibanData", {})
                        
                        response_message = (
                            f"✅ **هذا IBAN صحيح!**\n\n"
                            f"🏛 **البنك:** {bank_data.get('name', 'غير متوفر')}\n"
                            f"📍 **المدينة:** {bank_data.get('city', 'غير متوفر')}\n"
                            f"🌐 **الدولة:** {iban_data.get('country', 'غير متوفر')}\n"
                            f"🔠 **BIC / Swift:** {bank_data.get('bic', 'غير متوفر')}\n"
                            f"🔢 **رقم الحساب:** {bank_data.get('accountNumber', 'غير متوفر')}"
                        )
                    else:
                        response_message = "❌ **هذا الـ IBAN غير صحيح أو غير مسجل بشكل سليم.**"
                    
                    await update.message.reply_text(response_message, parse_mode="Markdown")
                else:
                    await update.message.reply_text("⚠️ حدث خطأ في الاتصال بخدمة التحقق.")
        except Exception as e:
            await update.message.reply_text("⚠️ حدث خطأ غير متوقع، عاود حاول لاحقاً.")

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), check_iban))
    print("البوت يعمل الآن...")
    app.run_polling()

if __name__ == "__main__":
    main()

