# -*- coding: utf-8 -*- # Add encoding declaration for clarity with Cyrillic
import logging
import os
import re # For basic VIN validation
import sys # Import sys for sys.exit

# --- Telegram, Supabase, Resend Libraries ---
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
)
from supabase import create_client, Client
import resend

# --- Configuration (Fetched from Environment Variables) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", 8080))

# --- Basic Configuration Check ---
required_vars = ["TELEGRAM_BOT_TOKEN", "SUPABASE_URL", "SUPABASE_KEY", "RESEND_API_KEY", "ADMIN_EMAIL", "WEBHOOK_SECRET", "RENDER_EXTERNAL_URL"]
missing_vars = [var_name for var_name in required_vars if os.environ.get(var_name) is None]
if missing_vars:
    logging.critical(f"Missing required environment variables: {', '.join(missing_vars)}. Bot cannot start.")
    sys.exit(1)

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("supabase").setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# --- Initialize Clients ---
supabase: Client | None = None
try:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY) # type: ignore
    logger.info("Supabase client initialized successfully.")
except Exception as e:
    logger.error(f"CRITICAL: Failed to initialize Supabase client: {e}")
    sys.exit(1)

try:
    resend.api_key = RESEND_API_KEY # type: ignore
    logger.info("Resend client configured successfully.")
except Exception as e:
    logger.error(f"CRITICAL: Failed to configure Resend client: {e}")
    pass

# --- Conversation States ---
ASK_VIN_KNOWN, GET_VIN, GET_CONTACT, GET_PARTS_VIN, GET_PARTS_CONTACT = range(5)

# --- Helper Functions ---

async def send_admin_notification(user_details: dict, order_details: dict):
    """Sends an email notification to the admin using Resend. (Email content remains English)"""
    if not RESEND_API_KEY or not ADMIN_EMAIL:
        logger.error("Resend API Key or Admin Email is not configured for sending notification.")
        return False

    from_address = "Parts Bot <bot@asiatek.pro>" # !! Use your verified domain email !!

    # --- Email content remains in English for the Admin ---
    subject = f"New Car Parts Request from {user_details.get('username', user_details['id'])}"
    html_body = f"""
    <h2>New Car Parts Request Received</h2>
    <p><strong>Telegram User ID:</strong> {user_details['id']}</p>
    <p><strong>Telegram Username:</strong> @{user_details.get('username', 'N/A')}</p>
    """
    if 'vin' in order_details and order_details['vin']:
        html_body += f"<p><strong>Provided VIN:</strong> {order_details['vin']}</p>"
    elif 'contact' in order_details and order_details['contact']:
         html_body += f"<p><strong>Provided Contact:</strong> {order_details['contact']}</p>"
    else:
         html_body += "<p><strong>VIN/Contact:</strong> Not provided or missing.</p>"
    html_body += f"<p><strong>Parts Needed:</strong></p><p>{order_details.get('parts', 'N/A')}</p>"
    html_body += "<hr><p>Please follow up with the user.</p>"
    # --- End of English Email Content ---

    try:
        params = {
            "from": from_address,
            "to": [ADMIN_EMAIL],
            "subject": subject,
            "html": html_body,
        }
        email = resend.Emails.send(params)
        logger.info(f"Admin notification email sent successfully via Resend: {email.get('id', 'N/A')}")
        return True
    except Exception as e:
        logger.error(f"Failed to send admin notification email via Resend. Error: {e}")
        logger.error(f"Resend params attempted: From={params.get('from')}, To={params.get('to')}, Subject={params.get('subject')}")
        return False

async def save_order_to_supabase(user_id: int, username: str | None, parts: str, vin: str | None = None, contact: str | None = None) -> bool:
    """Saves the order details to the Supabase 'orders' table."""
    if supabase is None:
        logger.error("Supabase client is not initialized. Cannot save order.")
        return False

    target_table = "orders"
    data = {
        "telegram_user_id": user_id,
        "telegram_username": username,
        "vin": vin,
        "contact_info": contact,
        "parts_needed": parts
    }
    data_to_insert = {k: v for k, v in data.items() if v is not None}
    logger.info(f"Attempting to insert data into '{target_table}': {data_to_insert}")

    try:
        supabase.table(target_table).insert(data_to_insert, returning=None).execute()
        logger.info(f"Supabase insert command executed successfully for user {user_id}.")
        return True
    except Exception as e:
        logger.error(f"Failed to save order to Supabase table '{target_table}' for user {user_id}. Data attempted: {data_to_insert}")
        error_message = f"General exception: {e}"
        if hasattr(e, 'message'): error_message += f" | Message: {e.message}" # type: ignore
        if hasattr(e, 'code'): error_message += f" | Code: {e.code}" # type: ignore
        if hasattr(e, 'details'): error_message += f" | Details: {e.details}" # type: ignore
        if hasattr(e, 'hint'): error_message += f" | Hint: {e.hint}" # type: ignore
        logger.error(f"Supabase error details: {error_message}")
        return False

# --- Command and Conversation Handlers (RUSSIAN TRANSLATIONS) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation and asks if the user knows their VIN (in Russian)."""
    user = update.effective_user
    if not user or not update.message:
        logger.warning("Received /start command from user object is None or message is None.")
        return ConversationHandler.END

    logger.info(f"User {user.id} ({user.username or 'NoUsername'}) started the bot.")

    # Russian: Welcome message
    await update.message.reply_html(
        f"👋 Добро пожаловать, {user.mention_html()}!\n\n"
        "Я помогу вам запросить автозапчасти. Для начала, пожалуйста, скажите:",
    )

    context.user_data['telegram_user_id'] = user.id
    context.user_data['telegram_username'] = user.username

    # Russian: Button labels (callback_data remains English!)
    keyboard = [
        [InlineKeyboardButton("✅ Да, я знаю свой VIN", callback_data="vin_yes")],
        [InlineKeyboardButton("❌ Нет, я не знаю свой VIN", callback_data="vin_no")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Russian: Ask about VIN
    await update.message.reply_text("Знаете ли вы VIN (идентификационный номер) вашего автомобиля?", reply_markup=reply_markup)

    return ASK_VIN_KNOWN

async def ask_vin_known_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the Yes/No answer about knowing the VIN (in Russian)."""
    query = update.callback_query
    if not query:
        logger.warning("ask_vin_known_handler received update without callback_query.")
        if update.effective_message:
             # Russian: Error message
             await update.effective_message.reply_text("Извините, я не понял. Пожалуйста, используйте кнопки или начните сначала с /start.")
        return ASK_VIN_KNOWN

    await query.answer()
    user_choice = query.data
    user = query.from_user

    context.user_data.setdefault('telegram_user_id', user.id)
    context.user_data.setdefault('telegram_username', user.username)

    if user_choice == "vin_yes":
        logger.info(f"User {user.id} chose 'Yes' to VIN.")
        # Russian: Ask for VIN
        await query.edit_message_text(text="Отлично! Пожалуйста, введите ваш 17-значный VIN.")
        return GET_VIN
    elif user_choice == "vin_no":
        logger.info(f"User {user.id} chose 'No' to VIN.")
        # Russian: Ask for contact info
        await query.edit_message_text(
            text="Нет проблем. Пожалуйста, укажите ваш номер телефона или адрес электронной почты, чтобы мы могли с вами связаться."
        )
        return GET_CONTACT
    else:
        logger.warning(f"User {user.id} sent unexpected callback data: {user_choice}")
        # Russian: Error message
        await query.edit_message_text(text="Извините, я не понял. Пожалуйста, используйте кнопки.")
        # Russian: Re-send buttons
        keyboard = [
            [InlineKeyboardButton("✅ Да, я знаю свой VIN", callback_data="vin_yes")],
            [InlineKeyboardButton("❌ Нет, я не знаю свой VIN", callback_data="vin_no")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if query.message:
            # Russian: Re-ask about VIN
            await query.message.reply_text("Знаете ли вы VIN вашего автомобиля?", reply_markup=reply_markup)
        return ASK_VIN_KNOWN

async def get_vin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores the VIN and asks for the parts needed (in Russian)."""
    user = update.effective_user
    if not user or not update.message or not update.message.text:
        logger.warning("get_vin received invalid update (no user or text message).")
        return GET_VIN

    user_vin = update.message.text.strip()
    logger.info(f"User {user.id} attempting to provide VIN: {user_vin}")

    if not re.match(r"^[A-HJ-NPR-Z0-9]{17}$", user_vin.upper()):
         logger.warning(f"User {user.id} provided invalid VIN format: {user_vin}")
         # Russian: Invalid VIN format message
         await update.message.reply_text(
             "Это не похоже на действительный 17-значный VIN (только буквы A-Z кроме I,O,Q и цифры 0-9).\nПожалуйста, попробуйте еще раз или введите /cancel для отмены."
         )
         return GET_VIN

    context.user_data['vin'] = user_vin.upper()
    logger.info(f"User {user.id} successfully provided VIN: {context.user_data['vin']}")

    # Russian: Ask for parts description
    await update.message.reply_text(
        "Спасибо! Теперь, пожалуйста, опишите необходимые вам автозапчасти или детали.",
         reply_markup=ReplyKeyboardRemove(),
    )
    return GET_PARTS_VIN

async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores the contact info and asks for the parts needed (in Russian)."""
    user = update.effective_user
    if not user or not update.message or not update.message.text:
        logger.warning("get_contact received invalid update (no user or text message).")
        return GET_CONTACT

    user_contact = update.message.text.strip()
    logger.info(f"User {user.id} attempting to provide contact: {user_contact}")

    if len(user_contact) < 5:
         logger.warning(f"User {user.id} provided short contact info: {user_contact}")
         # Russian: Invalid contact format message
         await update.message.reply_text(
            "Пожалуйста, введите действительный номер телефона или адрес электронной почты (минимум 5 символов).\nИли введите /cancel для отмены."
            )
         return GET_CONTACT

    context.user_data['contact'] = user_contact
    logger.info(f"User {user.id} successfully provided contact info.")

    # Russian: Ask for parts description
    await update.message.reply_text(
        "Понял! Теперь, пожалуйста, опишите необходимые вам автозапчасти или детали.",
         reply_markup=ReplyKeyboardRemove(),
    )
    return GET_PARTS_CONTACT

async def get_parts_vin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets parts description (after VIN), saves, notifies, and ends (in Russian)."""
    user = update.effective_user
    if not user or not update.message or not update.message.text:
        logger.warning("get_parts_vin received invalid update (no user or text message).")
        context.user_data.clear()
        return ConversationHandler.END

    parts_needed = update.message.text.strip()
    if not parts_needed:
        # Russian: Ask again for parts
        await update.message.reply_text("Пожалуйста, опишите необходимые детали или введите /cancel для отмены.")
        return GET_PARTS_VIN

    user_id = context.user_data.get('telegram_user_id')
    username = context.user_data.get('telegram_username')
    vin = context.user_data.get('vin')

    if user_id is None or vin is None:
         logger.error(f"Error: User data (ID:{user_id}, VIN:{vin}) missing in get_parts_vin context.")
         # Russian: Context error message
         await update.message.reply_text("Извините, произошла ошибка при получении ваших данных. Пожалуйста, начните сначала с /start.")
         context.user_data.clear()
         return ConversationHandler.END

    logger.info(f"User {user_id} (with VIN {vin}) needs parts: {parts_needed}")

    saved = await save_order_to_supabase(user_id, username, parts_needed, vin=vin)

    if saved:
        user_details = {"id": user_id, "username": username}
        order_details = {"vin": vin, "parts": parts_needed}
        notified = await send_admin_notification(user_details, order_details)

        # Russian: Success message (VIN path)
        await update.message.reply_text(
            "✅ Спасибо! Ваш запрос отправлен.\n"
            "Мы получили ваш VIN и список деталей. Мы обработаем его и свяжемся с вами при необходимости."
        )
        if not notified:
            # Russian: Notification failure warning
            await update.message.reply_text(
                 "(Возможно, возникла проблема с отправкой уведомления администратору по почте, но ваш запрос *сохранен*.)"
            )
    else:
        # Russian: Database save error message
        await update.message.reply_text(
            "❌ Извините, произошла ошибка при сохранении вашего запроса в базе данных. "
            "Пожалуйста, попробуйте позже или свяжитесь со службой поддержки напрямую, если проблема не исчезнет."
        )

    context.user_data.clear()
    return ConversationHandler.END

async def get_parts_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets parts description (after Contact), saves, notifies, and ends (in Russian)."""
    user = update.effective_user
    if not user or not update.message or not update.message.text:
        logger.warning("get_parts_contact received invalid update (no user or text message).")
        context.user_data.clear()
        return ConversationHandler.END

    parts_needed = update.message.text.strip()
    if not parts_needed:
        # Russian: Ask again for parts
        await update.message.reply_text("Пожалуйста, опишите необходимые детали или введите /cancel для отмены.")
        return GET_PARTS_CONTACT

    user_id = context.user_data.get('telegram_user_id')
    username = context.user_data.get('telegram_username')
    contact = context.user_data.get('contact')

    if user_id is None or contact is None:
         logger.error(f"Error: User data (ID:{user_id}, Contact:{contact}) missing in get_parts_contact context.")
         # Russian: Context error message
         await update.message.reply_text("Извините, произошла ошибка при получении ваших данных. Пожалуйста, начните сначала с /start.")
         context.user_data.clear()
         return ConversationHandler.END

    logger.info(f"User {user_id} (with contact {contact}) needs parts: {parts_needed}")

    saved = await save_order_to_supabase(user_id, username, parts_needed, contact=contact)

    if saved:
        user_details = {"id": user_id, "username": username}
        order_details = {"contact": contact, "parts": parts_needed}
        notified = await send_admin_notification(user_details, order_details)

        # Russian: Success message (Contact path)
        await update.message.reply_text(
            "✅ Спасибо! Ваш запрос отправлен.\n"
            "Мы получили ваши контактные данные и список деталей. Мы скоро свяжемся с вами!"
        )
        if not notified:
             # Russian: Notification failure warning
             await update.message.reply_text(
                 "(Возможно, возникла проблема с отправкой уведомления администратору по почте, но ваш запрос *сохранен*.)"
             )
    else:
        # Russian: Database save error message
        await update.message.reply_text(
            "❌ Извините, произошла ошибка при сохранении вашего запроса в базе данных. "
            "Пожалуйста, попробуйте позже или свяжитесь со службой поддержки напрямую, если проблема не исчезнет."
        )

    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation (in Russian)."""
    user = update.effective_user
    user_id_log = user.id if user else "Unknown"
    if update and update.effective_message:
        logger.info(f"User {user_id_log} canceled the conversation.")
        # Russian: Cancellation confirmation
        await update.effective_message.reply_text(
            "Хорошо, процесс запроса отменен.", reply_markup=ReplyKeyboardRemove()
        )
    else:
         logger.warning(f"Cancel received invalid update (no user or message). User ID: {user_id_log}")

    context.user_data.clear()
    return ConversationHandler.END

async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles messages that are not part of the expected conversation flow (in Russian)."""
    user_id = update.effective_user.id if update.effective_user else "Unknown"
    text = update.message.text if update.message else "[No message text]"
    state = context.conversation_state if hasattr(context, 'conversation_state') else 'N/A'
    logger.warning(f"Fallback handler triggered for user {user_id}. Message: '{text}'. State: {state}")
    if update and update.effective_message:
         # Russian: Fallback message
         await update.effective_message.reply_text(
            "Извините, я этого не ожидал. Если вы были в процессе запроса, пожалуйста, следуйте подсказкам. "
            "Вы всегда можете начать сначала с /start или отменить с /cancel."
         )

# --- Main Bot Execution ---
def main() -> None:
    """Start the bot using webhooks."""

    logger.info("Initializing Telegram Bot Application for Webhooks...")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build() # type: ignore

    # --- Add Handlers (Conversation logic remains the same) ---
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            # Callback data remains "vin_yes", "vin_no"
            ASK_VIN_KNOWN: [CallbackQueryHandler(ask_vin_known_handler, pattern="^vin_yes|vin_no$")],
            GET_VIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_vin)],
            GET_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_contact)],
            GET_PARTS_VIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_parts_vin)],
            GET_PARTS_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_parts_contact)],
        },
        fallbacks=[
             CommandHandler("cancel", cancel),
             MessageHandler(filters.ALL, fallback_handler)
        ],
        name="car_parts_conversation",
        persistent=False
    )

    application.add_handler(conv_handler)

    # --- Configure and Run Webhook ---
    webhook_url_path = "/webhook"
    if RENDER_EXTERNAL_URL:
        full_webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}{webhook_url_path}"
        logger.info(f"Webhook URL that will be set with Telegram: {full_webhook_url}")
    else:
        logger.critical("RENDER_EXTERNAL_URL is missing after initial check. Cannot set webhook URL.")
        sys.exit(1)

    logger.info(f"Starting webhook server on 0.0.0.0:{PORT}, listening for path {webhook_url_path}...")
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_url_path,
        webhook_url=full_webhook_url,
        secret_token=WEBHOOK_SECRET,
    )
    logger.info("Webhook server stopped.")

if __name__ == "__main__":
    main()