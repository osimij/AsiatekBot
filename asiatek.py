# bot.py – FINAL Version with Original Texts
# -*- coding: utf-8 -*-
import asyncio, json, logging, os, re, sys
from datetime import datetime
from typing import Any, Dict, Optional

from telegram import (
    Update, ReplyKeyboardRemove, InlineKeyboardButton,
    InlineKeyboardMarkup, constants
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)
from supabase import create_client, Client
import resend

# ---------- ENVIRONMENT ----------
REQ = [
    "TELEGRAM_BOT_TOKEN", "SUPABASE_URL", "SUPABASE_KEY",
    "RESEND_API_KEY", "ADMIN_EMAIL", "WEBHOOK_SECRET",
    "RENDER_EXTERNAL_URL"
]
missing = [v for v in REQ if not os.getenv(v)]
if missing:
    # Use logger if possible, otherwise print
    log_msg = f"Missing required environment variables: {', '.join(missing)}. Bot cannot start."
    try:
        logger.critical(log_msg)
    except NameError:
        print(f"CRITICAL: {log_msg}")
    sys.exit(1)

TG_TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
RESEND_KEY     = os.environ["RESEND_API_KEY"]
ADMIN_EMAIL    = os.environ["ADMIN_EMAIL"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
BASE_URL       = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
PORT           = int(os.getenv("PORT", 8080))

# ---------- LOGGING ----------
class JsonHandler(logging.StreamHandler):
    """Formats log records as single-line JSON strings."""
    def emit(self, record):
        try:
            log_entry = json.dumps({
                "t": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "lvl": record.levelname,
                "msg": self.format(record), # Use formatter for message string
                "mod": record.name,
                **(record.__dict__.get('exc_info') and \
                   {"exc_info": self.formatter.formatException(record.exc_info)} or {}),
            })
            self.stream.write(log_entry + "\n")
            self.flush() # Ensure logs are written out immediately
        except Exception:
            self.handleError(record)

# Configure root logger
log_formatter = logging.Formatter('%(message)s') # Basic message formatter
json_handler = JsonHandler()
json_handler.setFormatter(log_formatter)

root = logging.getLogger()
root.setLevel(logging.INFO)
root.handlers.clear() # Remove any default handlers
root.addHandler(json_handler)

# Silence excessive logging from libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("supabase").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logging.getLogger("telegram.bot").setLevel(logging.INFO)

logger = logging.getLogger("bot") # Specific logger for our bot

# ---------- CLIENTS ----------
supabase: Optional[Client] = None
try:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("Supabase client initialized.")
except Exception as e:
    logger.critical("Failed to initialize Supabase client.", exc_info=True)
    sys.exit(1) # Exit if Supabase is critical

try:
    resend.api_key = RESEND_KEY
    logger.info("Resend API key configured.")
except Exception as e:
    # Log error but don't exit, as Resend might be less critical
    logger.error("Failed to configure Resend client.", exc_info=True)

# ---------- STATES ----------
ASK_VIN_KNOWN, GET_VIN, GET_CONTACT, GET_PARTS = range(4)

# ---------- UTILITIES ----------
async def _run_sync_in_thread(func, *args, **kwargs):
    """Runs a synchronous function in the default thread pool executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

async def _insert_async(table: str, data: Dict[str, Any]) -> Any:
    """Non-blocking Supabase insert, returning the result."""
    if not supabase:
        logger.error("Supabase client not available for insert.")
        raise ConnectionError("Supabase client not initialized")
    # Run the blocking Supabase call in a thread
    response = await _run_sync_in_thread(
        supabase.table(table).insert(data, returning="minimal").execute
    )
    return response # Return the APIResponse object

async def log_interaction(
    update: Optional[Update],
    context: ContextTypes.DEFAULT_TYPE,
    interaction_type: str,
    detail: Optional[str] = None,
    user_id_override: Optional[int] = None,
):
    """Logs interaction details to Supabase."""
    user_id = username = first_name = None
    if update and update.effective_user:
        u = update.effective_user
        user_id, username, first_name = u.id, u.username, u.first_name
    elif user_id_override:
        user_id = user_id_override

    if not user_id:
        logger.warning("Cannot log interaction: No user information available.")
        return

    payload = {
        k: v
        for k, v in {
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "interaction_type": interaction_type,
            "interaction_detail": detail,
        }.items()
        if v is not None
    }

    # Schedule the background task
    try:
        task = context.application.create_task(
            _insert_async("bot_usage_log", payload),
            update=update,
            name=f"log_interaction_{user_id}_{interaction_type}"
        )
        task.add_done_callback(_handle_task_result) # Add callback for error logging
    except ConnectionError:
         logger.error("Failed to schedule log_interaction: Supabase client not ready.")
    except Exception as e:
         logger.error("Failed to schedule log_interaction task.", exc_info=True)

async def send_admin_notification(user: dict, order: dict):
    """Sends admin email notification using Resend."""
    if not RESEND_KEY or not ADMIN_EMAIL:
         logger.error("Resend not configured, skipping admin email.")
         return False # Indicate failure

    # --- Restored Original Email Format ---
    from_address = "Parts Bot <bot@asiatek.pro>"
    subject = "Получен новый запрос на автозапчасти"
    html_body = f"<h2>{subject}</h2><hr>"
    vin_info = order.get('vin')
    if vin_info: html_body += f"<p><strong>VIN:</strong> {vin_info}</p>"
    else: html_body += "<p><strong>VIN:</strong> Не был предоставлен пользователем.</p>"
    telegram_username = user.get('username', 'Не указано')
    contact_provided = order.get('contact', 'Контакт не был получен') # Use 'contact' key from order dict
    html_body += f"""
    <p><strong>ID пользователя Telegram:</strong> {user['id']}</p>
    <p><strong>Имя пользователя Telegram:</strong> @{telegram_username}</p>
    <p><strong>Предоставленные контакты:</strong> {contact_provided}</p><hr>"""
    parts_needed = order.get('parts', 'Не указаны') # Use 'parts' key from order dict
    html_body += f"""
    <p><strong>Необходимые запчасти:</strong></p>
    <blockquote style="border-left: 4px solid #ccc; padding-left: 10px; margin-left: 0; font-style: italic;">{parts_needed}</blockquote><hr>"""
    html_body += "<p>Пожалуйста, свяжитесь с пользователем.</p>"
    # --- End Restored Email Format ---

    params = {
        "from": from_address,
        "to": [ADMIN_EMAIL],
        "subject": subject,
        "html": html_body,
    }
    try:
        # Run blocking Resend call in a thread
        email_response = await _run_sync_in_thread(resend.Emails.send, params)
        email_id = email_response.get('id', 'N/A') if email_response else 'N/A'
        logger.info(f"Admin notification email sent successfully via Resend. ID: {email_id}")
        return True
    except Exception as e:
        logger.error("Failed to send admin notification email via Resend.", exc_info=True)
        # Log details that might have caused the failure
        logger.error(f"Resend params attempted: From={params.get('from')}, To={params.get('to')}, Subject={params.get('subject')}")
        return False

async def save_order_to_supabase(**data) -> bool:
    """Saves the order details to Supabase 'orders' table."""
    # Use the corrected keys for the check
    if not all(data.get(k) for k in ["telegram_user_id", "contact_info", "parts_needed"]):
        logger.error(f"Missing critical order data before save: {data}")
        return False

    # Remove None values before attempting insert
    data_to_insert = {k: v for k, v in data.items() if v is not None}
    user_id = data.get('telegram_user_id', 'Unknown') # For logging

    try:
        await _insert_async("orders", data_to_insert)
        logger.info(f"Order saved successfully to Supabase for user {user_id}.")
        return True
    except ConnectionError:
         logger.error(f"Failed to save order for user {user_id}: Supabase client not ready.")
         return False
    except Exception as e:
        # Log the actual Supabase/PostgREST error if available
        supabase_error_details = f"General exception: {e}"
        if hasattr(e, 'message'): supabase_error_details += f" | Message: {getattr(e, 'message')}"
        if hasattr(e, 'code'): supabase_error_details += f" | Code: {getattr(e, 'code')}"
        if hasattr(e, 'details'): supabase_error_details += f" | Details: {getattr(e, 'details')}"
        if hasattr(e, 'hint'): supabase_error_details += f" | Hint: {getattr(e, 'hint')}"
        logger.error(f"Failed to save order to Supabase for user {user_id}. Error: {supabase_error_details}", exc_info=True)
        return False

# ---------- HANDLERS (with restored original texts) ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts or restarts the conversation."""
    user = update.effective_user
    chat = update.effective_chat
    query = update.callback_query # Check if started via button

    if not user or not chat:
        logger.warning("Start command received without user or chat info.")
        return ConversationHandler.END # Cannot proceed

    is_restart = query is not None
    log_detail = 'new_request_button' if is_restart else '/start'
    await log_interaction(update, context, "command", log_detail)
    logger.info(f"User {user.id} ({user.username}) starting/restarting conversation via {log_detail}.")

    context.user_data.clear()
    context.user_data['id'] = user.id
    context.user_data['username'] = user.username
    context.user_data['vin'] = None # Initialize vin in context

    # --- Restored Original Texts ---
    welcome_text = f"👋 Снова здравствуйте, {user.mention_html()}!\n\nГотов принять новый запрос на автозапчасти. Для начала:" if is_restart else f"👋 Добро пожаловать, {user.mention_html()}!\n\nЯ помогу вам запросить автозапчасти. Для начала, пожалуйста, скажите:"
    ask_vin_text = "Знаете ли вы VIN (идентификационный номер) вашего автомобиля?"
    # --- End Restored Texts ---

    keyboard = [[InlineKeyboardButton("✅ Да, я знаю свой VIN", callback_data="vin_yes")],
                [InlineKeyboardButton("❌ Нет, я не знаю свой VIN", callback_data="vin_no")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if is_restart:
        await query.answer() # Acknowledge button press
        try:
            # Edit the message that had the button
            await query.edit_message_text(
                welcome_text + "\n\n" + ask_vin_text, # Combine texts for edit
                parse_mode=constants.ParseMode.HTML,
                reply_markup=reply_markup
            )
        except Exception as e:
            # Handle potential error if message can't be edited (e.g., too old)
            logger.error(f"Failed to edit message on restart for user {user.id}.", exc_info=True)
            # Send as new messages instead
            await chat.send_message(welcome_text, parse_mode=constants.ParseMode.HTML, reply_markup=ReplyKeyboardRemove())
            await chat.send_message(ask_vin_text, reply_markup=reply_markup)

    else:
        # Send welcome message first (remove keyboard from previous interactions)
        await chat.send_message(welcome_text, parse_mode=constants.ParseMode.HTML, reply_markup=ReplyKeyboardRemove())
        # Then send the question with buttons
        await chat.send_message(ask_vin_text, reply_markup=reply_markup)

    return ASK_VIN_KNOWN

async def ask_vin_known_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handles Yes/No VIN answer."""
    query = update.callback_query
    if not query or not query.from_user: return ConversationHandler.END
    await query.answer()
    await log_interaction(update, context, "callback_query", query.data)
    logger.info(f"User {query.from_user.id} answered VIN known: {query.data}")

    user_choice = query.data
    next_state = ConversationHandler.END
    # --- Restored Original Error Text ---
    reply_text = "Произошла ошибка. Пожалуйста, попробуйте начать сначала с /start."
    # --- End Restored Error Text ---

    if user_choice == "vin_yes":
        # --- Restored Original Text ---
        reply_text = "Отлично! Пожалуйста, введите ваш 17-значный VIN."
        # --- End Restored Text ---
        next_state = GET_VIN
    elif user_choice == "vin_no":
        context.user_data["vin"] = None # Explicitly set VIN to None if user says No
        # --- Restored Original Text ---
        reply_text = "Нет проблем. Пожалуйста, укажите ваш номер телефона или адрес электронной почты, чтобы мы могли с вами связаться."
        # --- End Restored Text ---
        next_state = GET_CONTACT
    else:
        logger.warning(f"Unexpected callback data in ask_vin_known: {query.data}")
        # Error text is already set

    try:
        await query.edit_message_text(text=reply_text)
    except Exception as e:
        logger.error(f"Failed to edit message in ask_vin_known for user {query.from_user.id}.", exc_info=True)
        # If edit fails, maybe send a new message? Or just proceed to next state.
        pass # Proceed to next state even if edit fails

    return next_state

async def get_vin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores VIN, asks for Contact Info."""
    user = update.effective_user
    message = update.message
    if not user or not message or not message.text:
        if update.effective_message:
            # --- Restored Original Text ---
            await update.effective_message.reply_text("Пожалуйста, введите ваш 17-значный VIN или /cancel для отмены.")
            # --- End Restored Text ---
        return GET_VIN # Stay in the same state

    user_vin = message.text.strip().upper()
    user_id = user.id

    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", user_vin): # Use fullmatch for exact 17 chars
         logger.warning(f"User {user_id} provided invalid VIN format: {user_vin}")
         # --- Restored Original Text ---
         await message.reply_text("Это не похоже на действительный 17-значный VIN.\nПожалуйста, попробуйте еще раз или введите /cancel для отмены.")
         # --- End Restored Text ---
         return GET_VIN # Ask again

    context.user_data['vin'] = user_vin
    await log_interaction(update, context, 'step_complete', 'vin_provided')
    logger.info(f"User {user_id} successfully provided VIN: {user_vin}") # Log the VIN
    # --- Restored Original Text ---
    await message.reply_text("Спасибо! Теперь, пожалуйста, укажите ваш номер телефона или адрес электронной почты для связи.", reply_markup=ReplyKeyboardRemove())
    # --- End Restored Text ---
    return GET_CONTACT

async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores Contact Info, asks for Parts."""
    user = update.effective_user
    message = update.message
    if not user or not message or not message.text:
        if update.effective_message:
             # --- Restored Original Text ---
            await update.effective_message.reply_text("Пожалуйста, укажите ваш номер телефона или адрес электронной почты, или /cancel для отмены.")
             # --- End Restored Text ---
        return GET_CONTACT # Stay in the same state

    user_contact = message.text.strip()
    user_id = user.id

    if len(user_contact) < 5: # Basic length validation
         logger.warning(f"User {user_id} provided short contact info: {user_contact}")
         # --- Restored Original Text ---
         await message.reply_text("Пожалуйста, введите действительный номер телефона или адрес электронной почты (минимум 5 символов).\nИли введите /cancel для отмены.")
         # --- End Restored Text ---
         return GET_CONTACT # Ask again

    context.user_data['contact'] = user_contact
    await log_interaction(update, context, 'step_complete', 'contact_provided')
    logger.info(f"User {user_id} successfully provided contact info.")
    # --- Restored Original Text ---
    await message.reply_text("Понял! Теперь, пожалуйста, опишите необходимые вам автозапчасти или детали.", reply_markup=ReplyKeyboardRemove())
    # --- End Restored Text ---
    return GET_PARTS

async def get_parts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets parts, saves, notifies, ends, AND provides 'Request Again' button."""
    user = update.effective_user
    message = update.message
    if not user or not message or not message.text:
        if update.effective_message:
             # --- Restored Original Text ---
            await update.effective_message.reply_text("Пожалуйста, опишите необходимые детали или введите /cancel для отмены.")
             # --- End Restored Text ---
        return GET_PARTS # Stay in the same state

    parts_needed = message.text.strip()
    user_id = user.id

    if not parts_needed:
         # --- Restored Original Text ---
        await message.reply_text("Пожалуйста, опишите необходимые детали или введите /cancel для отмены.")
         # --- End Restored Text ---
        return GET_PARTS # Ask again

    d = context.user_data
    # Ensure critical data exists from previous steps
    if "id" not in d or "contact" not in d:
         logger.error(f"Critical data missing in get_parts for user {user_id}. Context: {d}")
         # --- Restored Original Text ---
         await update.message.reply_text("Извините, произошла ошибка при получении ваших данных. Пожалуйста, начните сначала с /start.")
         # --- End Restored Text ---
         context.user_data.clear(); return ConversationHandler.END

    await log_interaction(update, context, 'step_complete', 'parts_provided')
    logger.info(f"User {user_id} described parts: '{parts_needed}'. Preparing to save.")

    # Prepare data for saving
    order_data = {
        "telegram_user_id": d["id"],
        "telegram_username": d.get("username"), # Username might be None
        "vin": d.get("vin"), # VIN might be None
        "contact_info": d["contact"], # Use the corrected key name
        "parts_needed": parts_needed, # Use the corrected key name
    }

    save_ok = await save_order_to_supabase(**order_data)

    # --- Restored Texts Based on Outcome ---
    if save_ok:
        logger.info(f"Order successfully saved for user {user_id}.")
        await log_interaction(update, context, "action_complete", "order_saved")

        # Attempt notification, log separately if it fails
        context.application.create_task(
             send_admin_notification(
                 {"id": d["id"], "username": d.get("username")},
                 # Pass correct keys for email generation
                 {"vin": d.get("vin"), "contact": d.get("contact"), "parts": parts_needed},
             ),
             update=update, name=f"send_admin_notification_{user_id}"
        ).add_done_callback(_handle_task_result)

        reply_text = "✅ Спасибо! Ваш запрос отправлен.\nМы получили ваши данные и список деталей. Мы скоро свяжемся с вами!"

    else:
        logger.error(f"Failed to save order for user {user_id}.")
        await log_interaction(update, context, "action_failed", "order_save_failed")
        # --- Restored Original Error Text ---
        reply_text = "❌ Извините, произошла ошибка при сохранении вашего запроса в базе данных. Пожалуйста, попробуйте позже или свяжитесь с администратором."
        # --- End Restored Error Text ---
    # --- End Restored Texts ---

    new_request_button = InlineKeyboardButton("➕ Запросить снова", callback_data="new_request")
    reply_markup_new_request = InlineKeyboardMarkup([[new_request_button]])

    await message.reply_text(reply_text, reply_markup=reply_markup_new_request)

    context.user_data.clear() # Clear data after finishing
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    user_id = update.effective_user.id if update.effective_user else "Unknown"
    await log_interaction(update, context, "command", "/cancel")
    logger.info(f"User {user_id} cancelled the conversation.")

    if update and update.effective_message:
         # --- Restored Original Text ---
        await update.effective_message.reply_text("Хорошо, процесс запроса отменен.", reply_markup=ReplyKeyboardRemove())
         # --- End Restored Text ---
    else:
         logger.warning(f"Cancel handler received invalid update object for user {user_id}.")

    context.user_data.clear()
    return ConversationHandler.END

async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles messages outside the expected flow or unexpected commands."""
    if not update or not update.effective_message:
        logger.warning("Fallback handler triggered with invalid update object.")
        return

    user_id = update.effective_user.id if update.effective_user else "Unknown"
    text = update.effective_message.text or "[No text message]"
    state = context.conversation_state if hasattr(context, 'conversation_state') else 'N/A'

    # Log the fallback event
    await log_interaction(update, context, "fallback", text[:100]) # Log first 100 chars
    logger.warning(f"Fallback handler triggered for user {user_id}. State: {state}. Message: '{text}'")

    # Check if the fallback is the keep-alive ping (now sent with secret token)
    # We let the standard reply happen to confirm receipt.
    if text == "ping" and update.effective_message.from_user is None: # Pings usually lack a user
        logger.info("Keep-alive ping received via fallback handler.")
        # You might want to exit silently here depending on desired behavior
        # return # Uncomment to send no reply to pings

    # --- Restored Original Texts ---
    if text.startswith('/'):
        reply_text = f"Команда {text} здесь не ожидается. Пожалуйста, следуйте инструкциям или используйте /cancel для отмены."
    else:
        # Generic response for unexpected text/media during conversation or outside it
        reply_text = "Извините, я этого не ожидал. Если вы были в процессе запроса, пожалуйста, следуйте подсказкам. Вы всегда можете начать сначала с /start или отменить с /cancel."
    # --- End Restored Texts ---

    try:
        await update.effective_message.reply_text(reply_text)
    except Exception as e:
        logger.error(f"Failed to send fallback reply to user {user_id}.", exc_info=True)


# ---------- ERROR HANDLING ----------
def _handle_task_result(task: asyncio.Task) -> None:
    """Callback function to log exceptions from background tasks."""
    try:
        task.result() # Retrieve result. If task raised exception, it's re-raised here.
    except asyncio.CancelledError:
        logger.warning(f"Background task '{task.get_name()}' was cancelled.")
    except Exception:
        # Log the exception with traceback
        logger.exception(f"Exception raised by background task '{task.get_name()}':")

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log Errors caused by Updates and notify user."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    # Inform user about the error, if possible
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Произошла внутренняя ошибка. Пожалуйста, попробуйте позже или /start.")
        except Exception as e:
            logger.error("Failed to send error message to user.", exc_info=True)


# ---------- MAIN ----------
def main() -> None:
    """Set up and run the bot."""
    logger.info("Starting bot application...")

    # Build the application
    application = (
        Application.builder()
        .token(TG_TOKEN)
        # NOTE: Removed post_init - caused issues with webhook startup timing
        .build()
    )

    # Register the global error handler
    application.add_error_handler(global_error_handler)

    # Conversation Handler Setup
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(start, pattern="^new_request$")
        ],
        states={
            ASK_VIN_KNOWN: [
                CallbackQueryHandler(ask_vin_known_handler, pattern="^vin_yes|vin_no$")
            ],
            GET_VIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_vin)],
            GET_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_contact)],
            GET_PARTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_parts)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            # Fallback handler specifically for messages within the conversation
            MessageHandler(filters.COMMAND | filters.ALL, fallback_handler)
        ],
        name="car_parts_conversation",
        persistent=False
    )
    application.add_handler(conv_handler, group=0) # Ensure conv handler runs first

    # Add a fallback handler outside the conversation (lower priority group)
    # This catches commands/messages sent when not in a conversation state.
    application.add_handler(MessageHandler(filters.COMMAND | filters.ALL, fallback_handler), group=1)

    # --- Webhook Setup ---
    webhook_path = "/webhook"
    full_webhook_url = f"{BASE_URL}{webhook_path}"
    logger.info(f"Setting webhook URL: {full_webhook_url}")
    logger.info(f"Webhook server listening on 0.0.0.0:{PORT} for path {webhook_path}")

    # Run the webhook server, ensuring secret_token is included
    try:
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=webhook_path,
            webhook_url=full_webhook_url,
            secret_token=WEBHOOK_SECRET, # Crucial for security
            # NOTE: post_init removed from here (invalid syntax)
        )
    except Exception as e:
        logger.critical("Failed to start webhook server.", exc_info=True)
        sys.exit(1)

    logger.info("Webhook server stopped.") # Usually seen on shutdown signal

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Catch critical startup errors not handled elsewhere
        logger.critical("Application failed to start.", exc_info=True)
        sys.exit(1)
