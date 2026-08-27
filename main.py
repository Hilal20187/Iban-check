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

async def delete_webhook_safely(token):
    url = f"https://api.telegram.org/bot{token}/deleteWebhook?drop_pending_updates=true"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as resp:
                pass
        except:
            pass

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    raw_lines = text.split('\n')
    ibans = []
    for line in raw_lines:
        parts = line.strip().split()
        for p in parts:
            clean_p = p.replace(" ", "").upper()
            if len(clean_p) >= 15 and clean_p[:2].isalpha():
                if clean_p not in ibans:
                    ibans.append(clean_p)

    if not ibans:
        clean_single = text.replace(" ", "").upper()
        if len(clean_single) >= 15:
            ibans = [clean_single]
        else:
            await update.message.reply_text("❌ يرجى إرسال رقم IBAN صالح واحد على الأقل أو قائمة أرقام.")
            return

    wait_msg = await update.message.reply_text(f"⏳ جاري فحص وتحقق {len(ibans)} من الـ IBANs فعلياً...")

    async with aiohttp.ClientSession() as session:
        results_output = []
        
        for iban in ibans:
            url = f"https://openiban.com/validate/{iban}?getBIC=true"
            
            try:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        if data.get("valid") == True:
                            bank_data = data.get("bankData", {})
                            bank_name = bank_data.get("name") or "غير متوفر"
                            bic = bank_data.get("bic") or "غير متوفر"
                            city = bank_data.get("city") or "غير متوفر"
                            
                            sepa_instant_supported = False
                            instant_keywords = ["REVOLT", "N26", "WISE", "PBNK", "DEUT", "COBA"]
                            if any(k in bic.upper() for k in instant_keywords) or any(k in bank_name.upper() for k in ["REVOLT", "N26", "WISE"]):
                                sepa_instant_supported = True
                            
                            instant_status = "⚡ SEPA Instant: مدعوم (Supported)" if sepa_instant_supported else "❌ SEPA Instant: غير مدعوم (Not supported)"

                            results_output.append(
                                f"✅ `{iban}`\n"
                                f"• البنك: {bank_name}\n"
                                f"• BIC: `{bic}`\n"
                                f"• المدينة: {city}\n"
                                f"• {instant_status}\n"
                                f"-----------------------------------"
                            )
                        else:
                            results_output.append(
                                f"❌ `{iban}`\n"
                                f"• الحالة: هذا الـ IBAN غير صالح أو غير موجود (Invalid IBAN)\n"
                                f"-----------------------------------"
                            )
                    else:
                        results_output.append(f"⚠️ `{iban}`: تعذر التحقق من الخادم.")
            except:
                results_output.append(f"⚠️ `{iban}`: خطأ في الاتصال بالخدمة.")

        final_text = f"📋 **نتائج التحقق الفعلي ({len(ibans)} أرقام):**\n\n" + "\n".join(results_output)
        
        if len(final_text) > 4000:
            final_text = final_text[:4000] + "\n\n... (تم اقتصاص القائمة لطولها الزائد)"

        await wait_msg.edit_text(final_text, parse_mode="Markdown")

def main():
    if not TOKEN:
        return

    import asyncio
    asyncio.run(delete_webhook_safely(TOKEN))

    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    application.run_polling()

if __name__ == "__main__":
    main()
 
