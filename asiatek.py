# -*- coding: utf-8 -*- # Add encoding declaration for clarity with Cyrillic
import logging
import os
import re # For basic VIN validation
import sys # Import sys for sys.exit

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

async def send_admin_notification(user_details: dict, order_details: dict):
    """Sends an email notification to the admin using Resend (Revised Russian Format)."""
    if not RESEND_API_KEY or not ADMIN_EMAIL:
        logger.error("Resend API Key or Admin Email is not configured for sending notification.")
        return False

    from_address = "Parts Bot <bot@asiatek.pro>" # !! Use your verified domain email !!
    subject = "Получен новый запрос на автозапчасти"
    html_body = f"<h2>{subject}</h2><hr>"
    vin_info = order_details.get('vin')
    if vin_info:
        html_body += f"<p><strong>VIN:</strong> {vin_info}</p>"
    else:
        html_body += "<p><strong>VIN:</strong> Не был предоставлен пользователем.</p>"
    telegram_username = user_details.get('username', 'Не указано')
    contact_provided = order_details.get('contact', 'Контакт не был получен')
    html_body += f"""
    <p><strong>ID пользователя Telegram:</strong> {user_details['id']}</p>
    <p><strong>Имя пользователя Telegram:</strong> @{telegram_username}</p>
    <p><strong>Предоставленные контакты:</strong> {contact_provided}</p>
    <hr>
    """
    parts_needed = order_details.get('parts', 'Не указаны')
    html_body += f"""
    <p><strong>Необходимые запчасти:</strong></p>
    <blockquote style="border-left: 4px solid #ccc; padding-left: 10px; margin-left: 0; font-style: italic;">
        {parts_needed}
    </blockquote>
    <hr>
    """
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
    if supabase is None:
        logger.error("Supabase client is not initialized. Cannot save order.")
        return False
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
        error_message = f"General exception: {e}"
        if hasattr(e, 'message'): error_message += f" | Message: {e.message}" # type: ignore
        if hasattr(e, 'code'): error_message += f" | Code: {e.code}" # type: ignore
        if hasattr(e, 'details'): error_message += f" | Details: {e.details}" # type: ignore
        if hasattr(e, 'hint'): error_message += f" | Hint: {e.hint}" # type: ignore
        logger.error(f"Supabase error details: {error_message}")
        return False

# --- Command and Conversation Handlers ---

# *** REVISED: start function handles command and callback ***
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts/Restarts the conversation via command or callback button."""
    user = update.effective_user
    query = update.callback_query
    chat = update.effective_chat # Get chat object

    if not user or not chat: # Basic checks
        logger.warning("Start triggered with no effective user or chat.")
        if query: await query.answer() # Answer callback even if failing
        return ConversationHandler.END

    # Acknowledge button press if applicable
    if query:
        await query.answer()
        # Optional: Edit the original message to remove the button after clicking
        # try:
        #     await query.edit_message_reply_markup(reply_markup=None)
        # except Exception as e:
        #     logger.warning(f"Could not edit previous message markup: {e}")

    logger.info(f"User {user.id} starting/restarting request (via {'button' if query else 'command'}).")

    # Clear previous conversation data for a fresh start
    context.user_data.clear()
    context.user_data['telegram_user_id'] = user.id
    context.user_data['telegram_username'] = user.username
    context.user_data['vin'] = None # Initialize VIN as None

    # Prepare messages
    welcome_text = f"👋 Снова здравствуйте, {user.mention_html()}!\n\nГотов принять новый запрос на автозапчасти. Для начала:" if query else f"👋 Добро пожаловать, {user.mention_html()}!\n\nЯ помогу вам запросить автозапчасти. Для начала, пожалуйста, скажите:"
    ask_vin_text = "Знаете ли вы VIN (идентификационный номер) вашего автомобиля?"
    keyboard = [
        [InlineKeyboardButton("✅ Да, я знаю свой VIN", callback_data="vin_yes")],
        [InlineKeyboardButton("❌ Нет, я не знаю свой VIN", callback_data="vin_no")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Send messages directly to the chat
    await chat.send_message(welcome_text, parse_mode=constants.ParseMode.HTML, reply_markup=ReplyKeyboardRemove())
    await chat.send_message(ask_vin_text, reply_markup=reply_markup)

    return ASK_VIN_KNOWN


async def ask_vin_known_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles Yes/No VIN answer."""
    query = update.callback_query
    if not query: return ASK_VIN_KNOWN # Should not happen with inline buttons
    await query.answer()
    user = query.from_user
    context.user_data.setdefault('telegram_user_id', user.id)
    context.user_data.setdefault('telegram_username', user.username)
    context.user_data.setdefault('vin', None)
    user_choice = query.data
    if user_choice == "vin_yes":
        logger.info(f"User {user.id} chose 'Yes' to VIN.")
        await query.edit_message_text(text="Отлично! Пожалуйста, введите ваш 17-значный VIN.")
        return GET_VIN
    elif user_choice == "vin_no":
        logger.info(f"User {user.id} chose 'No' to VIN.")
        await query.edit_message_text(
            text="Нет проблем. Пожалуйста, укажите ваш номер телефона или адрес электронной почты, чтобы мы могли с вами связаться."
        )
        return GET_CONTACT
    else: # Should not happen with pattern matching
        logger.warning(f"User {user.id} sent unexpected callback data: {user_choice}")
        await query.edit_message_text(text="Произошла ошибка. Пожалуйста, попробуйте начать сначала с /start.")
        return ConversationHandler.END


async def get_vin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores VIN, asks for Contact Info."""
    user = update.effective_user
    message = update.message
    if not user or not message or not message.text:
        # Re-prompt if message is invalid
        if update.effective_message:
            await update.effective_message.reply_text("Пожалуйста, введите ваш 17-значный VIN или /cancel для отмены.")
        return GET_VIN

    user_vin = message.text.strip()
    if not re.match(r"^[A-HJ-NPR-Z0-9]{17}$", user_vin.upper()):
         logger.warning(f"User {user.id} provided invalid VIN format: {user_vin}")
         await message.reply_text(
             "Это не похоже на действительный 17-значный VIN.\nПожалуйста, попробуйте еще раз или введите /cancel для отмены."
         )
         return GET_VIN
    context.user_data['vin'] = user_vin.upper()
    logger.info(f"User {user.id} successfully provided VIN: {context.user_data['vin']}")
    await message.reply_text(
        "Спасибо! Теперь, пожалуйста, укажите ваш номер телефона или адрес электронной почты для связи.",
         reply_markup=ReplyKeyboardRemove(),
    )
    return GET_CONTACT


async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores Contact Info, asks for Parts."""
    user = update.effective_user
    message = update.message
    if not user or not message or not message.text:
        if update.effective_message:
            await update.effective_message.reply_text("Пожалуйста, укажите ваш номер телефона или адрес электронной почты, или /cancel для отмены.")
        return GET_CONTACT

    user_contact = message.text.strip()
    if len(user_contact) < 5:
         logger.warning(f"User {user.id} provided short contact info: {user_contact}")
         await message.reply_text(
            "Пожалуйста, введите действительный номер телефона или адрес электронной почты (минимум 5 символов).\nИли введите /cancel для отмены."
            )
         return GET_CONTACT
    context.user_data['contact'] = user_contact
    logger.info(f"User {user.id} successfully provided contact info.")
    await message.reply_text(
        "Понял! Теперь, пожалуйста, опишите необходимые вам автозапчасти или детали.",
         reply_markup=ReplyKeyboardRemove(),
    )
    return GET_PARTS


async def get_parts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets parts, saves, notifies, ends, AND provides 'Request Again' button."""
    user = update.effective_user
    message = update.message
    if not user or not message or not message.text:
        if update.effective_message:
             await update.effective_message.reply_text("Пожалуйста, опишите необходимые детали или введите /cancel для отмены.")
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

    saved = await save_order_to_supabase(user_id=user_id, username=username, parts=parts_needed, contact=contact, vin=vin)

    # *** ADDED: 'Request Again' button ***
    new_request_button = InlineKeyboardButton("➕ Запросить снова", callback_data="new_request")
    reply_markup_new_request = InlineKeyboardMarkup([[new_request_button]])

    if saved:
        user_details = {"id": user_id, "username": username}
        order_details = {"vin": vin, "contact": contact, "parts": parts_needed}
        notified = await send_admin_notification(user_details, order_details)

        # Send success message WITH the new button
        await message.reply_text(
            "✅ Спасибо! Ваш запрос отправлен.\n"
            "Мы получили ваши данные и список деталей. Мы скоро свяжемся с вами!",
            reply_markup=reply_markup_new_request # Add the button here
        )
        if not notified:
             # Also add button if notification failed but save worked
             await message.reply_text(
                 "(Возможно, возникла проблема с отправкой уведомления администратору по почте, но ваш запрос *сохранен*.)",
                 reply_markup=reply_markup_new_request
             )
    else:
        # Add button even if save failed, so user can retry easily
        await message.reply_text(
            "❌ Извините, произошла ошибка при сохранении вашего запроса в базе данных. "
            "Пожалуйста, попробуйте позже или свяжитесь со службой поддержки напрямую, если проблема не исчезнет.",
             reply_markup=reply_markup_new_request
        )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    user = update.effective_user
    user_id_log = user.id if user else "Unknown"
    if update and update.effective_message:
        logger.info(f"User {user_id_log} canceled the conversation.")
        await update.effective_message.reply_text(
            "Хорошо, процесс запроса отменен.", reply_markup=ReplyKeyboardRemove()
        )
    else:
         logger.warning(f"Cancel received invalid update (no user or message). User ID: {user_id_log}")
    context.user_data.clear()
    return ConversationHandler.END


async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles messages outside the expected flow."""
    user_id = update.effective_user.id if update.effective_user else "Unknown"
    text = update.message.text if update.message else "[No message text]"
    state = context.conversation_state if hasattr(context, 'conversation_state') else 'N/A'
    logger.warning(f"Fallback handler triggered for user {user_id}. Message: '{text}'. State: {state}")
    if update and update.effective_message:
         if text.startswith('/'):
             await update.effective_message.reply_text(
                 f"Команда {text} здесь не ожидается. Пожалуйста, следуйте инструкциям или используйте /cancel для отмены."
             )
         else:
            await update.effective_message.reply_text(
                "Извините, я этого не ожидал. Если вы были в процессе запроса, пожалуйста, следуйте подсказкам. "
                "Вы всегда можете начать сначала с /start или отменить с /cancel."
            )


# --- Main Bot Execution ---
def main() -> None:
    """Start the bot using webhooks."""
    logger.info("Initializing Telegram Bot Application for Webhooks...")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build() # type: ignore

    # --- Add Handlers (REVISED entry_points) ---
    conv_handler = ConversationHandler(
        # *** UPDATED: Added CallbackQueryHandler for 'new_request' button ***
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(start, pattern="^new_request$") # Button triggers start func
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