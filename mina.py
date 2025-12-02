import os
import re
import json
import html
import logging
import bcrypt
import qrcode
import firebase_admin
from PIL import Image
from firebase_admin import credentials, firestore
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters
)

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8250676956:AAFDai-LuCrY8yPfxFprlkOGbxuBAJObddw")
FIREBASE_CRED_PATH = "serviceAccountKey.json"
FIREBASE_ENV_VAR = "FIREBASE_SERVICE_ACCOUNT"

# ---------------------------------------------------------
# LOGGING & FIREBASE SETUP
# ---------------------------------------------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize Firebase
if not firebase_admin._apps:
    try:
        # Check for environment variable first (Railway/Cloud)
        firebase_json = os.getenv(FIREBASE_ENV_VAR)
        
        if firebase_json:
            logger.info("Using Firebase credentials from environment variable.")
            cred_dict = json.loads(firebase_json)
            cred = credentials.Certificate(cred_dict)
        elif os.path.exists(FIREBASE_CRED_PATH):
            logger.info("Using Firebase credentials from local file.")
            cred = credentials.Certificate(FIREBASE_CRED_PATH)
        else:
            raise FileNotFoundError("No Firebase credentials found (Env var or File).")

        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("Firebase initialized.")
    except Exception as e:
        logger.error(f"Firebase init failed: {e}")
        exit(1)
else:
    db = firestore.client()

# ---------------------------------------------------------
# SECURITY HELPERS
# ---------------------------------------------------------

def hash_password(plain_password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(plain_password.encode('utf-8'), salt).decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

# DECORATOR: Forces user to log in before using specific commands
def login_required(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not context.user_data.get('logged_in'):
            await update.message.reply_text(
                "⛔ <b>Access Denied</b>\n"
                "You must log in to use QR features.\n"
                "Use: <code>/login email password</code>", 
                parse_mode=ParseMode.HTML
            )
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

def get_data_type(text):
    if re.match(r'^https?://', text): return "Website URL 🌐"
    elif text.startswith("WIFI:"): return "WiFi Network 📶"
    elif text.startswith("mailto:"): return "Email Address 📧"
    elif text.startswith("tel:"): return "Phone Number 📞"
    else: return "Text/Data 📝"

# ---------------------------------------------------------
# AUTH COMMANDS
# ---------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # We escape the name to prevent errors if name contains < or >
    user = html.escape(update.effective_user.first_name)
    
    await update.message.reply_text(
        f"👋 Hi <b>{user}</b>! Welcome to the <b>Secure QR Bot</b>.\n\n"
        "<b>🔐 Auth Commands:</b>\n"
        "• <code>/register email pass</code>\n"
        "• <code>/login email pass</code>\n"
        "• <code>/logout</code>\n\n"
        "<b>🎨 QR Commands (Login Required):</b>\n"
        "• Send Text/Link → Get QR\n"
        "• Send Photo → Set Logo\n"
        "• <code>/hd</code> → Toggle HD Mode\n"
        "• <code>/color red white</code>\n"
        "• <code>/reset</code>",
        parse_mode=ParseMode.HTML
    )

async def register_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("⚠️ Usage: <code>/register email password</code>", parse_mode=ParseMode.HTML)
        return
    
    email, password = args[0], args[1]
    users_ref = db.collection('users')

    # Check for existing user
    if any(users_ref.where('email', '==', email).limit(1).stream()):
        await update.message.reply_text("❌ Email already exists.")
        return

    # Save to Firestore
    try:
        users_ref.add({
            'email': email,
            'password': hash_password(password),
            'created_at': firestore.SERVER_TIMESTAMP
        })
        await update.message.reply_text("✅ Registered! Now please <code>/login</code>.", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Register Error: {e}")
        await update.message.reply_text("❌ Error saving to database.")

async def login_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("⚠️ Usage: <code>/login email password</code>", parse_mode=ParseMode.HTML)
        return

    email, password = args[0], args[1]
    users_ref = db.collection('users')
    
    try:
        query = list(users_ref.where('email', '==', email).limit(1).stream())

        if not query:
            await update.message.reply_text("❌ Email not found.")
            return

        user_data = query[0].to_dict()
        if verify_password(password, user_data['password']):
            context.user_data['logged_in'] = True
            context.user_data['email'] = email
            await update.message.reply_text("🔓 <b>Login Successful!</b>\nYou can now generate QR codes.", parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text("❌ Wrong password.")
            
    except Exception as e:
        logger.error(f"Login Error: {e}")
        await update.message.reply_text("❌ System error during login.")

async def logout_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🔒 Logged out.")

@login_required
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    email = context.user_data.get('email')
    # Using HTML allows underscores in emails without crashing
    await update.message.reply_text(f"👤 <b>Profile</b>\nLogged in as: <code>{email}</code>", parse_mode=ParseMode.HTML)

# ---------------------------------------------------------
# QR GENERATION COMMANDS (PROTECTED)
# ---------------------------------------------------------

@login_required
async def toggle_hd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['hd'] = not context.user_data.get('hd', False)
    state = "ON" if context.user_data['hd'] else "OFF"
    await update.message.reply_text(f"📸 HD Mode: <b>{state}</b>", parse_mode=ParseMode.HTML)

@login_required
async def set_color(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("Usage: <code>/color red white</code>", parse_mode=ParseMode.HTML)
        return
    context.user_data['fill'] = context.args[0]
    context.user_data['back'] = context.args[1]
    await update.message.reply_text(f"🎨 Colors set: {context.args[0]} on {context.args[1]}")

@login_required
async def reset_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Keep login state, reset QR settings
    email = context.user_data.get('email')
    context.user_data.clear()
    context.user_data['logged_in'] = True
    context.user_data['email'] = email
    await update.message.reply_text("🔄 QR settings reset.")

@login_required
async def handle_logo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo_file = await update.message.photo[-1].get_file()
    logo_path = f"logo_{user_id}.png"
    await photo_file.download_to_drive(logo_path)
    context.user_data['logo_path'] = logo_path
    await update.message.reply_text("🖼️ Logo uploaded! It will appear on your QRs.")

@login_required
async def generate_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    data_type = get_data_type(text)
    
    # Get settings
    hd = context.user_data.get('hd', False)
    fill = context.user_data.get('fill', 'black')
    back = context.user_data.get('back', 'white')
    logo_path = context.user_data.get('logo_path')
    
    filename = f"qr_{user_id}.png"
    status = await update.message.reply_text("⏳ Generating...")

    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=20 if hd else 10,
            border=4
        )
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color=fill, back_color=back).convert('RGB')

        if logo_path and os.path.exists(logo_path):
            try:
                logo = Image.open(logo_path)
                qr_w, qr_h = img.size
                logo_size = int(qr_w * 0.25)
                logo = logo.resize((logo_size, logo_size), Image.Resampling.LANCZOS)
                pos = ((qr_w - logo_size) // 2, (qr_h - logo_size) // 2)
                img.paste(logo, pos)
            except Exception as e:
                logger.error(f"Logo error: {e}")

        img.save(filename)
        
        # We use HTML here too to be safe
        await update.message.reply_photo(
            photo=open(filename, 'rb'),
            caption=f"✅ <b>{data_type}</b>\nQuality: {'HD' if hd else 'Normal'}",
            parse_mode=ParseMode.HTML
        )
        await context.bot.delete_message(update.effective_chat.id, status.message_id)

    except Exception as e:
        logger.error(f"QR Gen Error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        if os.path.exists(filename): os.remove(filename)

# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
if __name__ == '__main__':
    if not os.getenv(FIREBASE_ENV_VAR) and not os.path.exists(FIREBASE_CRED_PATH):
        print("⚠️ Missing serviceAccountKey.json or FIREBASE_SERVICE_ACCOUNT env var")
        exit(1)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Public
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('register', register_user))
    app.add_handler(CommandHandler('login', login_user))
    app.add_handler(CommandHandler('logout', logout_user))
    
    # Protected (QR)
    app.add_handler(CommandHandler('profile', profile))
    app.add_handler(CommandHandler('hd', toggle_hd))
    app.add_handler(CommandHandler('color', set_color))
    app.add_handler(CommandHandler('reset', reset_settings))
    app.add_handler(MessageHandler(filters.PHOTO, handle_logo))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), generate_qr))

    print("Ultimate Bot Running...")

    app.run_polling()
