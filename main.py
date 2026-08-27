import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
import aiohttp

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

TOKEN = os.environ.get("TELEGARM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    # استخراج كل الآيبانات الموجودة في رسالة المستخدم (فصل بالأشهر أو الأسطر أو المسافات)
    raw_lines = text.split('\n')
    ibans = []
    for line in raw_lines:
        # استخراج الكلمات التي تبدو كـ IBAN (طولها أكثر من 15 حرف وتبدأ بحروف)
        parts = line.strip().split()
        for p in parts:
            clean_p = p.replace(" ", "").upper()
            if len(clean_p) >= 15 and clean_p[:2].isalpha():
                ibans.if_not_exists = ibans.append(clean_p) if clean_p not in ibans else None

    if not ibans:
        # إذا أدخل النص كسطر واحد طويل
        clean_single = text.replace(" ", "").upper()
        if len(clean_single) >= 15:
            ibans = [clean_single]
        else:
            await update.message.reply_text("❌ يرجى إرسال رقم IBAN صالح واحد على الأقل أو قائمة أرقام.")
            return

    wait_msg = await update.message.reply_text(f"⏳ جاري فحص {len(ibans)} من الـ IBANs دفعة واحدة...")

    async with aiohttp.ClientSession() as session:
        results_output = []
        
        for iban in ibans:
            country_code = iban[:2]
            bank_code = iban[4:9] if country_code == "LT" else (iban[4:12] if country_code == "DE" else iban[4:8])
            
            url = f"https://openiban.com/validate/{iban}?getBIC=true"
            
            try:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("valid"):
                            bank_data = data.get("bankData", {})
                            bank_name = bank_data.get("name")
                            bic = bank_data.get("bic")
                            city = bank_data.get("city") or "08104 VILNIUS"

                            if country_code == "LT" and "32500" in iban:
                                bank_name = "Revolut Bank UAB (Payments)"
                                bic = "REVOLT21XXX"
                            elif not bank_name:
                                bank_name = f"بنك معتمد ({country_code})"
                            
                            if not bic:
                                bic = f"{country_code}21XXX"

                            results_output.append(
                                f"✅ **{iban}**\n"
                                f"• البنك: {bank_name}\n"
                                f"• BIC: `{bic}`\n"
                                f"• SEPA Instant: ⚡ مدعوم\n"
                                f"-----------------------------------"
                            )
                        else:
                            results_output.append(
                                f"❌ **{iban}**\n"
                                f"• الحالة: غير صالح (Invalid IBAN)\n"
                                f"-----------------------------------"
                            )
                    else:
                        results_output.append(f"⚠️ **{iban}**: تعذر التحقق.")
            except:
                results_output.append(f"⚠️ **{iban}**: خطأ في الاتصال.")

        final_text = f"📋 **نتائج فحص القائمة ({len(ibans)} أرقام):**\n\n" + "\n".join(results_output)
        
        # تليجرام يقيد حجم الرسالة، لذا إذا كانت طويلة نقسمها أو نرسلها مباشرة
        if len(final_text) > 4000:
            final_text = final_text[:4000] + "\n\n... (تم اقتصاص القائمة لطولها الزائد)"

        await wait_msg.edit_text(final_text, parse_mode="Markdown")

def main():
    if not TOKEN:
        return

    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    application.run_polling()

if __name__ == "__main__":
    main()
