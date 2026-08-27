import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

# تفعيل سجلات التتبع لمتابعة الأخطاء إن وجدت
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# قراءة التوكن بشكل مخفي وآمن من إعدادات النظام في المنصة (Render)
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """دالة لمعالجة أي رسالة نصية أو IBAN يرسلها المستخدم"""
  user_text = update.message.text
  # يمكنك هنا إضافة الكود الخاص بفحص أو معالجة الـ IBAN
  await update.message.reply_text(
      f"وصلتني رسالتك بنجاح وقمت باستلام النص:\n`{user_text}`",
      parse_mode="Markdown",
  )


def main():
  if not TOKEN:
    print(
        "خطأ: لم يتم العثور على التوكن! تأكد من إضافته في متغيرات البيئة على"
        " المنصة."
    )
    return

  # بناء وتجهيز تطبيق البوت
  application = ApplicationBuilder().token(TOKEN).build()

  # إضافة معالج للرسائل النصية (مثل الـ IBAN أو /start وغيرها)
  application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

  # تشغيل البوت
  print("البوت يعمل الآن...")
  application.run_polling()


if __name__ == "__main__":
  main()
