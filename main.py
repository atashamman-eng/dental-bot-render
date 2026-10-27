import os
import sqlite3
import asyncio
import logging
import base64
import io
from contextlib import closing

from flask import Flask, request, abort, has_request_context
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, filters
from telegram.constants import ChatAction
from PIL import Image, ImageDraw

BOT_TOKEN = "9167303334:AAFLe7R0vgkRMkM_xOg6daJEa7zREYTeyDA"
ROBOFLOW_API_KEY = "4v0Xo9ITSJXPQuZfdt3v"
ROBOFLOW_MODEL_ID = "atashs-workspace/dental-caries-v1"

VISION_API_KEYS = [
    "AIzaSyBdvg21i_aglG5ZqtADbpPlTnD7B6T-0CI",
    "AQ.Ab8RN6Ji0qLYYKLyaa0Xsho_sv7O2zwbiNIp_vFGR-74S6ts4w"
]
CHAT_API_KEYS = [
    "AQ.Ab8RN6I4pMf8NOeiYgNdjVj8jqWpB_EQOFP2x6Bf32L5YPBPxg",
    "AQ.Ab8RN6L96rwWKNHhpYWExt3syoYc_U6tU3aZIrqP-WGaZVYELA"
]

DB_PATH = "/tmp/dental_bot.db"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

SERVICES = ("crown", "implant", "bridge")
NEW_NAME, NEW_TYPE, NEW_COLOR, NEW_COUNT, WAITING_PRICE, WAITING_IMAGE, WAITING_AI = range(7)
MENU_BUTTONS_REGEX = "^(👥 Patients|🆕 New Patient|💰 Prices|🩺 Specialist Doctor|🤖 AI)$"

def _conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    with closing(_conn()) as conn, conn:
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS prices (service TEXT PRIMARY KEY, price REAL NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS patients (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, service TEXT NOT NULL, tooth_color TEXT NOT NULL, count INTEGER NOT NULL, total_price REAL NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        for s in SERVICES:
            c.execute("INSERT OR IGNORE INTO prices (service, price) VALUES (?, 0)", (s,))

def db_get_price(service):
    with closing(_conn()) as conn:
        row = conn.execute("SELECT price FROM prices WHERE service=?", (service,)).fetchone()
    return row[0] if row else 0.0

def db_all_prices():
    with closing(_conn()) as conn:
        rows = conn.execute("SELECT service, price FROM prices").fetchall()
    return {s: p for s, p in rows}

def db_set_price(service, price):
    with closing(_conn()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO prices (service, price) VALUES (?, ?)", (service, price))

def db_add_patient(name, service, color, count, total):
    with closing(_conn()) as conn, conn:
        conn.execute("INSERT INTO patients (name, service, tooth_color, count, total_price) VALUES (?, ?, ?, ?, ?)", (name, service, color, count, total))

def db_get_patients():
    with closing(_conn()) as conn:
        return conn.execute("SELECT name, service, tooth_color, count, total_price, created_at FROM patients ORDER BY id DESC").fetchall()

def fmt_price(p):
    s = f"{p:.2f}"
    return s[:-3] if s.endswith(".00") else s

SERVICE_LABELS = {"crown": "👑 Crown", "implant": "🔩 Implant", "bridge": "🌉 Bridge"}

def main_menu_kb():
    return ReplyKeyboardMarkup([["👥 Patients", "🆕 New Patient"], ["💰 Prices", "🩺 Specialist Doctor"], ["🤖 AI"]], resize_keyboard=True)

def service_inline_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("👑 Crown", callback_data="svc_crown"), InlineKeyboardButton("🔩 Implant", callback_data="svc_implant"), InlineKeyboardButton("🌉 Bridge", callback_data="svc_bridge")]])

def price_inline_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("👑 Crown", callback_data="price_crown"), InlineKeyboardButton("🔩 Implant", callback_data="price_implant"), InlineKeyboardButton("🌉 Bridge", callback_data="price_bridge")]])

def regen_kb(file_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Regenerate", callback_data=f"regen:{file_id}")]])

async def cmd_start(update, context):
    await update.message.reply_text("🦷 *Welcome to Dental Bot!*\n\nChoose an option from the menu below:", parse_mode="Markdown", reply_markup=main_menu_kb())

async def cmd_help(update, context):
    await update.message.reply_text("🦷 *Help*\n\n/start -> Main menu\n/cancel -> Cancel current action\n\nUse the menu buttons to navigate.", parse_mode="Markdown", reply_markup=main_menu_kb())

async def cmd_cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled. Back to main menu.", reply_markup=main_menu_kb())
    return ConversationHandler.END

async def show_patients(update, context):
    rows = db_get_patients()
    if not rows:
        await update.message.reply_text("📭 No patients registered yet.")
        return
    lines = [f"👥 *Patients List* - {len(rows)} total\n"]
    for i, (name, service, color, count, total, _) in enumerate(rows, 1):
        lines.append(f"*{i}. {name}*\n   🦷 {SERVICE_LABELS.get(service, service)} x {count}\n   🎨 {color}\n   💰 Total: {fmt_price(total)}\n")
    full = "\n".join(lines)
    if len(full) <= 4000:
        await update.message.reply_text(full, parse_mode="Markdown")
    else:
        await update.message.reply_text("Too many patients to display.", parse_mode="Markdown")

async def show_prices(update, context):
    c = db_all_prices()
    text = f"💰 *Prices*\n\n👑 Crown:   {fmt_price(c.get('crown', 0))}\n🔩 Implant: {fmt_price(c.get('implant', 0))}\n🌉 Bridge:  {fmt_price(c.get('bridge', 0))}\n\nSelect a service to update its price:"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=price_inline_kb())

async def price_service_chosen(update, context):
    q = update.callback_query
    await q.answer()
    service = q.data.replace("price_", "")
    context.user_data["pending_service"] = service
    await q.edit_message_text(f"💵 Current *{SERVICE_LABELS[service]}* price: {fmt_price(db_get_price(service))}\n\nSend the new price (number only):", parse_mode="Markdown")
    return WAITING_PRICE

async def price_input(update, context):
    service = context.user_data.get("pending_service")
    if not service:
        await update.message.reply_text("⚠️ No service selected. Use 💰 Prices again.", reply_markup=main_menu_kb())
        return ConversationHandler.END
    txt = update.message.text.strip().replace(",", "")
    try:
        price = float(txt)
        if price < 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid number. Send a positive number:")
        return WAITING_PRICE
    db_set_price(service, price)
    context.user_data.pop("pending_service", None)
    await update.message.reply_text(f"✅ *{SERVICE_LABELS[service]}* price set to {fmt_price(price)}.", parse_mode="Markdown", reply_markup=main_menu_kb())
    return ConversationHandler.END

async def new_patient_start(update, context):
    context.user_data["new_patient"] = {}
    await update.message.reply_text("🆕 *New Patient*\n\nStep 1/4 - Enter the patient's name:", parse_mode="Markdown")
    return NEW_NAME

async def new_patient_name(update, context):
    name = update.message.text.strip()
    if not name or len(name) > 100:
        await update.message.reply_text("❌ Invalid name (max 100 chars). Try again:")
        return NEW_NAME
    context.user_data["new_patient"] = {"name": name}
    await update.message.reply_text(f"👤 Patient: *{name}*\n\nStep 2/4 - Choose the service:", parse_mode="Markdown", reply_markup=service_inline_kb())
    return NEW_TYPE

async def new_patient_type(update, context):
    q = update.callback_query
    await q.answer()
    service = q.data.replace("svc_", "")
    context.user_data.setdefault("new_patient", {})["service"] = service
    await q.edit_message_text(f"🦷 Service: *{SERVICE_LABELS[service]}*\n\nStep 3/4 - Enter the tooth color (e.g. A2, B1):", parse_mode="Markdown")
    return NEW_COLOR

async def new_patient_color(update, context):
    color = update.message.text.strip()
    if not color or len(color) > 50:
        await update.message.reply_text("❌ Invalid color. Try again:")
        return NEW_COLOR
    context.user_data["new_patient"]["color"] = color
    await update.message.reply_text(f"🎨 Color: *{color}*\n\nStep 4/4 - Enter the number of units:", parse_mode="Markdown")
    return NEW_COUNT

async def new_patient_count(update, context):
    txt = update.message.text.strip()
    try:
        count = int(txt)
        if count <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid count. Send a positive integer:")
        return NEW_COUNT
    data = context.user_data.get("new_patient", {})
    if not all(k in data for k in ("name", "service", "color")):
        await update.message.reply_text("⚠️ Something went wrong. Please start over.", reply_markup=main_menu_kb())
        return ConversationHandler.END
    unit = db_get_price(data["service"])
    total = unit * count
    db_add_patient(data["name"], data["service"], data["color"], count, total)
    summary = f"✅ *Patient Saved*\n\n👤 Name: {data['name']}\n🦷 Service: {SERVICE_LABELS[data['service']]}\n🎨 Tooth Color: {data['color']}\n🔢 Count: {count}\n💵 Unit Price: {fmt_price(unit)}\n💰 *Total: {fmt_price(total)}*"
    await update.message.reply_text(summary, parse_mode="Markdown", reply_markup=main_menu_kb())
    context.user_data.clear()
    return ConversationHandler.END

async def ai_start(update, context):
    await update.message.reply_text("🤖 *AI Assistant*\n\nYou can now chat with me! Type your message or /cancel to exit.", parse_mode="Markdown")
    return WAITING_AI

async def handle_ai_message(update, context):
    user_text = update.message.text
    await update.message.reply_chat_action(ChatAction.TYPING)
    msg = await update.message.reply_text("⏳")
    payload = {"contents": [{"parts": [{"text": user_text}]}]}
    
    success = False
    for api_key in CHAT_API_KEYS:
        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
        try:
            resp = await asyncio.to_thread(requests.post, gemini_url, json=payload, timeout=20)
            data = resp.json()
            if resp.status_code == 200 and "candidates" in data:
                await msg.edit_text(data["candidates"][0]["content"]["parts"][0]["text"][:4000])
                success = True
                break
        except:
            pass
    if not success:
        await msg.edit_text("❌ Error: All API keys failed.")
    return WAITING_AI

async def specialist_start(update, context):
    await update.message.reply_text("🩺 *Specialist Doctor*\n\nPlease send the dental X-Ray image (JPG/PNG).", parse_mode="Markdown")
    return WAITING_IMAGE

def split_message(text, max_length):
    if len(text) <= max_length:
        return [text]
    parts = []
    while len(text) > 0:
        split_idx = text.rfind('\n', 0, max_length)
        if split_idx == -1:
            split_idx = max_length
        parts.append(text[:split_idx])
        text = text[split_idx:].lstrip()
    return parts

async def _execute_image_analysis(update, context, file_id, is_callback=False):
    msg_obj = update.callback_query.message if is_callback else update.message
    if is_callback: await update.callback_query.answer()
    
    progress_msg = await msg_obj.reply_text("⏳")
    await context.bot.send_chat_action(chat_id=msg_obj.chat_id, action=ChatAction.TYPING)

    try:
        new_file = await context.bot.get_file(file_id)
        image_bytes = await new_file.download_as_bytearray()
        img_b64 = base64.b64encode(image_bytes).decode('utf-8')
        
        rf_url = f"https://detect.roboflow.com/{ROBOFLOW_MODEL_ID}?api_key={ROBOFLOW_API_KEY}"
        rf_resp = await asyncio.to_thread(requests.post, rf_url, files={"file": image_bytes}, data={"confidence": 60}, timeout=20)
        predictions = rf_resp.json().get("predictions", [])
        
        yolo_summary = "No caries detected."
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)

        if predictions:
            yolo_summary = "YOLO detected: "
            for p in predictions:
                yolo_summary += f"{p['class']} ({p['confidence']:.2f}), "
                color = "red" if "caries" in p['class'].lower() else ("blue" if "implant" in p['class'].lower() else "yellow")
                x, y, w, h = p['x']-p['width']/2, p['y']-p['height']/2, p['width'], p['height']
                draw.rectangle([x, y, x+w, y+h], outline=color, width=6)
            yolo_summary = yolo_summary.rstrip(", ")

        output_buffer = io.BytesIO()
        img.save(output_buffer, format="JPEG")
        output_buffer.seek(0)

        prompt = f"You are an expert dental radiologist. YOLO findings: {yolo_summary}. Generate a concise English report. DO NOT use Markdown symbols like ** or #."
        payload = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}}]}]}
        
        final_text = "Error generating report."
        for api_key in VISION_API_KEYS:
            gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
            resp = await asyncio.to_thread(requests.post, gemini_url, json=payload, timeout=30)
            data = resp.json()
            if resp.status_code == 200 and "candidates" in data:
                final_text = data["candidates"][0]["content"]["parts"][0]["text"].replace("# ", "").replace("**", "").replace("*", "")
                break

        await progress_msg.delete()
        caption = "📸 Result:\n\n" + final_text
        await msg_obj.reply_photo(photo=output_buffer, caption=caption[:1024])
        if len(caption) > 1024:
            await msg_obj.reply_text(caption[1024:])

    except Exception as e:
        try: await progress_msg.delete()
        except: pass
        await msg_obj.reply_text(f"❌ Process failed: {str(e)}\n\nClick below to try again.", reply_markup=regen_kb(file_id))

async def process_dental_image(update, context):
    file_id = update.message.photo[-1].file_id if update.message.photo else update.message.document.file_id
    await _execute_image_analysis(update, context, file_id, is_callback=False)
    return WAITING_IMAGE

async def regenerate_callback(update, context):
    file_id = update.callback_query.data.replace("regen:", "")
    await update.callback_query.edit_message_text("🔄 Regenerating...")
    await _execute_image_analysis(update, context, file_id, is_callback=True)
    return WAITING_IMAGE

async def escape_to_menu(update, context):
    context.user_data.clear()
    text = update.message.text
    if text == "👥 Patients": await show_patients(update, context)
    elif text == "💰 Prices": await show_prices(update, context)
    elif text == "🩺 Specialist Doctor": return await specialist_start(update, context)
    elif text == "🤖 AI": return await ai_start(update, context)
    elif text == "🆕 New Patient": return await new_patient_start(update, context)
    return ConversationHandler.END

async def unknown(update, context):
    await update.message.reply_text("🤔 I didn't understand that.", reply_markup=main_menu_kb())

def build_application():
    init_db()
    ptb = Application.builder().token(BOT_TOKEN).build()
    
    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^💰 Prices$"), show_prices),
            MessageHandler(filters.Regex("^🆕 New Patient$"), new_patient_start),
            MessageHandler(filters.Regex("^👥 Patients$"), show_patients),
            MessageHandler(filters.Regex("^🩺 Specialist Doctor$"), specialist_start),
            MessageHandler(filters.Regex("^🤖 AI$"), ai_start),
            CallbackQueryHandler(price_service_chosen, pattern="^price_"),
        ],
        states={
            WAITING_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(MENU_BUTTONS_REGEX), price_input)],
            NEW_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(MENU_BUTTONS_REGEX), new_patient_name)],
            NEW_TYPE: [CallbackQueryHandler(new_patient_type, pattern="^svc_")],
            NEW_COLOR: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(MENU_BUTTONS_REGEX), new_patient_color)],
            NEW_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(MENU_BUTTONS_REGEX), new_patient_count)],
            WAITING_IMAGE: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, process_dental_image), CallbackQueryHandler(regenerate_callback, pattern="^regen:")],
            WAITING_AI: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(MENU_BUTTONS_REGEX), handle_ai_message)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel), CommandHandler("start", cmd_cancel), MessageHandler(filters.Regex(MENU_BUTTONS_REGEX), escape_to_menu)],
    )
    ptb.add_handler(CommandHandler("start", cmd_start))
    ptb.add_handler(conv)
    ptb.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))
    return ptb

app = Flask(__name__)
_ptb_app = None
_loop = None
_webhook_set = False

def _ensure_app():
    global _ptb_app, _loop
    if _ptb_app is not None: return _ptb_app, _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _ptb_app = build_application()
    _loop.run_until_complete(_ptb_app.initialize())
    return _ptb_app, _loop

def _maybe_set_webhook():
    global _webhook_set
    if _webhook_set: return
    base = request.host_url.rstrip("/")
    try:
        ptb_app, loop = _ensure_app()
        loop.run_until_complete(ptb_app.bot.set_webhook(url=f"{base}/webhook", drop_pending_updates=True))
        _webhook_set = True
    except Exception as e:
        logger.error(f"Webhook setup failed: {e}")

@app.route("/", methods=["GET"])
def index():
    _maybe_set_webhook()
    return "Dental Bot is running on Vercel!"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        ptb_app, loop = _ensure_app()
        update = Update.de_json(request.get_json(force=True), ptb_app.bot)
        if update is not None: 
            loop.run_until_complete(ptb_app.process_update(update))
    except Exception as e:
        logger.error(f"Webhook error: {e}")
    return "ok", 200
