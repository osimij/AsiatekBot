# -*- coding: utf-8 -*-
import logging
from telegram.ext import ApplicationHandlerStop
import os
import re
import sys
from datetime import datetime # Import datetime

from aiohttp import web
import asyncio
import threading

# --- Telegram, Supabase, Resend Libraries ---
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup, constants
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
# Optional: Add ADMIN_USER_ID if you implement /stats command
# ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID")

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
ASK_VIN_KNOWN, GET_VIN, GET_CONTACT, GET_PARTS = range(4)

# --- Helper Functions ---

# *** ADDED: Interaction Logging Function ***
async def log_interaction(
    update: Update | None,  # Optional
    context: ContextTypes.DEFAULT_TYPE,
    interaction_type: str,  # Required
    detail: str | None = None,  # Optional
    user_id_override: int | None = None  # Optional
):

    """Logs user interaction details to the Supabase 'bot_usage_log' table."""
    if supabase is None:
        # logger.warning("Supabase client not initialized. Skipping interaction log.") # Can be noisy
        return

    user_id = None
    username = None
    first_name = None

    if update and update.effective_user:
        user = update.effective_user
        user_id = user.id
        username = user.username
        first_name = user.first_name
    elif user_id_override:
         user_id = user_id_override
    else:
        # logger.warning("No user information available to log interaction.") # Can be noisy
        return

    log_data = {
        "user_id": user_id,
        "username": username,
        "first_name": first_name,
        "interaction_type": interaction_type,
        "interaction_detail": detail,
    }
    log_data_insert = {k: v for k, v in log_data.items() if v is not None}

    try:
        # Use create_task for non-blocking insert
        context.application.create_task(
             supabase.table("bot_usage_log").insert(log_data_insert, returning=None).execute(),
             update=update
        )
    except Exception as e:
        logger.error(f"Failed to log interaction to Supabase for user {user_id}. Error: {e}")


async def send_admin_notification(user_details: dict, order_details: dict):
    """Sends an email notification to the admin using Resend (Revised Russian Format)."""
    if not RESEND_API_KEY or not ADMIN_EMAIL:
        logger.error("Resend API Key or Admin Email is not configured for sending notification.")
        return False

    from_address = "Parts Bot <bot@asiatek.pro>"
    subject = "Получен новый запрос на автозапчасти"
    html_body = f"<h2>{subject}</h2><hr>"
    vin_info = order_details.get('vin')
    if vin_info: html_body += f"<p><strong>VIN:</strong> {vin_info}</p>"
    else: html_body += "<p><strong>VIN:</strong> Не был предоставлен пользователем.</p>"
    telegram_username = user_details.get('username', 'Не указано')
    contact_provided = order_details.get('contact', 'Контакт не был получен')
    html_body += f"""
    <p><strong>ID пользователя Telegram:</strong> {user_details['id']}</p>
    <p><strong>Имя пользователя Telegram:</strong> @{telegram_username}</p>
    <p><strong>Предоставленные контакты:</strong> {contact_provided}</p><hr>"""
    parts_needed = order_details.get('parts', 'Не указаны')
    html_body += f"""
    <p><strong>Необходимые запчасти:</strong></p>
    <blockquote style="border-left: 4px solid #ccc; padding-left: 10px; margin-left: 0; font-style: italic;">{parts_needed}</blockquote><hr>"""
    html_body += "<p>Пожалуйста, свяжитесь с пользователем.</p>"
    try:
        params = {"from": from_address, "to": [ADMIN_EMAIL], "subject": subject, "html": html_body}
        email = resend.Emails.send(params)
        logger.info(f"Admin notification email sent successfully via Resend: {email.get('id', 'N/A')}")
        return True
    except Exception as e:
        logger.error(f"Failed to send admin notification email via Resend. Error: {e}")
        logger.error(f"Resend params attempted: From={params.get('from')}, To={params.get('to')}, Subject={params.get('subject')}")
        return False


async def save_order_to_supabase(user_id: int, username: str | None, parts: str, contact: str | None = None, vin: str | None = None) -> bool:
    """Saves the order details to the Supabase 'orders' table."""
    if supabase is None: return False
    target_table = "orders"
    data = {"telegram_user_id": user_id, "telegram_username": username, "vin": vin, "contact_info": contact, "parts_needed": parts}
    data_to_insert = {k: v for k, v in data.items() if v is not None}
    logger.info(f"Attempting to insert data into '{target_table}': {data_to_insert}")
    try:
        supabase.table(target_table).insert(data_to_insert, returning=None).execute()
        logger.info(f"Supabase insert command executed successfully for user {user_id}.")
        return True
    except Exception as e:
        logger.error(f"Failed to save order to Supabase table '{target_table}' for user {user_id}. Data attempted: {data_to_insert}")
        # Log detailed error if available
        error_message = f"General exception: {e}"
        if hasattr(e, 'message'): error_message += f" | Message: {e.message}" # type: ignore
        if hasattr(e, 'code'): error_message += f" | Code: {e.code}" # type: ignore
        if hasattr(e, 'details'): error_message += f" | Details: {e.details}" # type: ignore
        if hasattr(e, 'hint'): error_message += f" | Hint: {e.hint}" # type: ignore
        logger.error(f"Supabase error details: {error_message}")
        return False


# --- Command and Conversation Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts/Restarts the conversation."""
    user = update.effective_user
    query = update.callback_query
    chat = update.effective_chat
    if not user or not chat: return ConversationHandler.END

    # *** LOGGING: Log start/restart ***
    log_type = 'callback_restart' if query else 'command'
    log_detail = 'new_request' if query else '/start'
    await log_interaction(update, context, interaction_type=log_type, detail=log_detail)

    if query: await query.answer() # Acknowledge button
    logger.info(f"User {user.id} starting/restarting request (via {'button' if query else 'command'}).")

    context.user_data.clear()
    context.user_data['telegram_user_id'] = user.id
    context.user_data['telegram_username'] = user.username
    context.user_data['vin'] = None

    welcome_text = f"👋 Снова здравствуйте, {user.mention_html()}!\n\nГотов принять новый запрос на автозапчасти. Для начала:" if query else f"👋 Добро пожаловать, {user.mention_html()}!\n\nЯ помогу вам запросить автозапчасти. Для начала, пожалуйста, скажите:"
    ask_vin_text = "Знаете ли вы VIN (идентификационный номер) вашего автомобиля?"
    keyboard = [[InlineKeyboardButton("✅ Да, я знаю свой VIN", callback_data="vin_yes")], [InlineKeyboardButton("❌ Нет, я не знаю свой VIN", callback_data="vin_no")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await chat.send_message(welcome_text, parse_mode=constants.ParseMode.HTML, reply_markup=ReplyKeyboardRemove())
    await chat.send_message(ask_vin_text, reply_markup=reply_markup)
    return ASK_VIN_KNOWN


async def ask_vin_known_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles Yes/No VIN answer."""
    query = update.callback_query
    if not query: return ASK_VIN_KNOWN
    await query.answer()
    user = query.from_user
    context.user_data.setdefault('telegram_user_id', user.id)
    context.user_data.setdefault('telegram_username', user.username)
    context.user_data.setdefault('vin', None)

    # *** LOGGING: Log button press ***
    await log_interaction(update, context, interaction_type='callback_query', detail=query.data)

    user_choice = query.data
    if user_choice == "vin_yes":
        logger.info(f"User {user.id} chose 'Yes' to VIN.")
        await query.edit_message_text(text="Отлично! Пожалуйста, введите ваш 17-значный VIN.")
        return GET_VIN
    elif user_choice == "vin_no":
        logger.info(f"User {user.id} chose 'No' to VIN.")
        await query.edit_message_text(text="Нет проблем. Пожалуйста, укажите ваш номер телефона или адрес электронной почты, чтобы мы могли с вами связаться.")
        return GET_CONTACT
    else:
        logger.warning(f"User {user.id} sent unexpected callback data: {user_choice}")
        await query.edit_message_text(text="Произошла ошибка. Пожалуйста, попробуйте начать сначала с /start.")
        return ConversationHandler.END


async def get_vin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores VIN, asks for Contact Info."""
    user = update.effective_user
    message = update.message
    if not user or not message or not message.text:
        if update.effective_message: await update.effective_message.reply_text("Пожалуйста, введите ваш 17-значный VIN или /cancel для отмены.")
        return GET_VIN
    user_vin = message.text.strip()
    if not re.match(r"^[A-HJ-NPR-Z0-9]{17}$", user_vin.upper()):
         logger.warning(f"User {user.id} provided invalid VIN format: {user_vin}")
         await message.reply_text("Это не похоже на действительный 17-значный VIN.\nПожалуйста, попробуйте еще раз или введите /cancel для отмены.")
         return GET_VIN
    context.user_data['vin'] = user_vin.upper()
    logger.info(f"User {user.id} successfully provided VIN: {context.user_data['vin']}")

    # *** LOGGING: Log VIN provided ***
    await log_interaction(update, context, interaction_type='action_completed', detail='vin_provided')

    await message.reply_text("Спасибо! Теперь, пожалуйста, укажите ваш номер телефона или адрес электронной почты для связи.", reply_markup=ReplyKeyboardRemove())
    return GET_CONTACT


async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores Contact Info, asks for Parts."""
    user = update.effective_user
    message = update.message
    if not user or not message or not message.text:
        if update.effective_message: await update.effective_message.reply_text("Пожалуйста, укажите ваш номер телефона или адрес электронной почты, или /cancel для отмены.")
        return GET_CONTACT
    user_contact = message.text.strip()
    if len(user_contact) < 5:
         logger.warning(f"User {user.id} provided short contact info: {user_contact}")
         await message.reply_text("Пожалуйста, введите действительный номер телефона или адрес электронной почты (минимум 5 символов).\nИли введите /cancel для отмены.")
         return GET_CONTACT
    context.user_data['contact'] = user_contact
    logger.info(f"User {user.id} successfully provided contact info.")

    # *** LOGGING: Log contact provided ***
    await log_interaction(update, context, interaction_type='action_completed', detail='contact_provided')

    await message.reply_text("Понял! Теперь, пожалуйста, опишите необходимые вам автозапчасти или детали.", reply_markup=ReplyKeyboardRemove())
    return GET_PARTS


async def get_parts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets parts, saves, notifies, ends, AND provides 'Request Again' button."""
    user = update.effective_user
    message = update.message
    if not user or not message or not message.text:
        if update.effective_message: await update.effective_message.reply_text("Пожалуйста, опишите необходимые детали или введите /cancel для отмены.")
        return GET_PARTS
    parts_needed = message.text.strip()
    if not parts_needed:
        await message.reply_text("Пожалуйста, опишите необходимые детали или введите /cancel для отмены.")
        return GET_PARTS

    user_id = context.user_data.get('telegram_user_id')
    username = context.user_data.get('telegram_username')
    vin = context.user_data.get('vin')
    contact = context.user_data.get('contact')

    if user_id is None or contact is None:
         logger.error(f"Error: Critical data missing (ID:{user_id}, Contact:{contact}) in get_parts context.")
         await message.reply_text("Извините, произошла ошибка при получении ваших данных. Пожалуйста, начните сначала с /start.")
         context.user_data.clear()
         return ConversationHandler.END

    logger.info(f"User {user_id} needs parts: {parts_needed}. VIN: {vin}, Contact: {contact}")

    # *** LOGGING: Log parts provided ***
    await log_interaction(update, context, interaction_type='action_completed', detail='parts_provided')

    saved = await save_order_to_supabase(user_id=user_id, username=username, parts=parts_needed, contact=contact, vin=vin)

    new_request_button = InlineKeyboardButton("➕ Запросить снова", callback_data="new_request")
    reply_markup_new_request = InlineKeyboardMarkup([[new_request_button]])

    if saved:
        # *** LOGGING: Log successful save ***
        await log_interaction(update, context, interaction_type='action_completed', detail='order_saved_successfully')

        user_details = {"id": user_id, "username": username}
        order_details = {"vin": vin, "contact": contact, "parts": parts_needed}
        notified = await send_admin_notification(user_details, order_details)

        await message.reply_text("✅ Спасибо! Ваш запрос отправлен.\nМы получили ваши данные и список деталей. Мы скоро свяжемся с вами!", reply_markup=reply_markup_new_request)
        if not notified:
             await message.reply_text("(Возможно, возникла проблема с отправкой уведомления администратору по почте, но ваш запрос *сохранен*.)", reply_markup=reply_markup_new_request)
    else:
        # *** LOGGING: Log failed save ***
        await log_interaction(update, context, interaction_type='action_failed', detail='order_save_failed')
        await message.reply_text("❌ Извините, произошла ошибка при сохранении вашего запроса в базе данных.", reply_markup=reply_markup_new_request)

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    user = update.effective_user
    user_id_log = user.id if user else "Unknown"

    # *** LOGGING: Log cancel command ***
    # Use user_id_log if user object might be None here
    await log_interaction(update, context, user_id_override=user_id_log if not user else None, interaction_type='command', detail='/cancel')

    if update and update.effective_message:
        logger.info(f"User {user_id_log} canceled the conversation.")
        await update.effective_message.reply_text("Хорошо, процесс запроса отменен.", reply_markup=ReplyKeyboardRemove())
    else:
         logger.warning(f"Cancel received invalid update (no user or message). User ID: {user_id_log}")
    context.user_data.clear()
    return ConversationHandler.END


async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles messages outside the expected flow."""

    # --- Handle keep-alive ping silently ---
    if update and update.effective_message and update.effective_message.text == "ping":
        logger.info("Received keep-alive ping — ignoring.")
        return

    # --- Fallback logic for unexpected inputs ---
    user = update.effective_user
    user_id = user.id if user else "Unknown"
    text = update.message.text if update.message else "[No message text]"
    state = context.conversation_state if hasattr(context, 'conversation_state') else 'N/A'
    logger.warning(f"Fallback handler triggered for user {user_id}. Message: '{text}'. State: {state}")

    # Log fallback interaction
    await log_interaction(update, context, interaction_type='fallback', detail=text[:100])  # Log first 100 chars

    if update and update.effective_message:
        if text.startswith('/'):
            await update.effective_message.reply_text(
                f"Команда {text} здесь не ожидается. Пожалуйста, следуйте инструкциям или используйте /cancel для отмены.")
        else:
            await update.effective_message.reply_text(
                "Извините, я этого не ожидал. Если вы были в процессе запроса, пожалуйста, следуйте подсказкам. "
                "Вы всегда можете начать сначала с /start или отменить с /cancel.")
            
async def keep_alive_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"🔥 KEEP-ALIVE PING RECEIVED: {update.to_dict()}")
    raise ApplicationHandlerStop()


# --- Main Bot Execution ---
def main() -> None:
    """Start the bot using webhooks."""
    logger.info("Initializing Telegram Bot Application for Webhooks...")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Add keep-alive route to internal aiohttp app
    async def handle_keep_alive(request):
        return web.json_response({"status": "awake"})

    application.web_app.router.add_get("/keep-alive", handle_keep_alive)

    # Handle GitHub keep-alive ping
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex("^ping$"), keep_alive_handler),
        group=0
    )

    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(start, pattern="^new_request$")
        ],
        states={
            ASK_VIN_KNOWN: [CallbackQueryHandler(ask_vin_known_handler, pattern="^vin_yes|vin_no$")],
            GET_VIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_vin)],
            GET_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_contact)],
            GET_PARTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_parts)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.COMMAND, fallback_handler),
            MessageHandler(filters.ALL, fallback_handler)
        ],
        name="car_parts_conversation",
        persistent=False
    )
    application.add_handler(conv_handler)

    # Webhook setup
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
