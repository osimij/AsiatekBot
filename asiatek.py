# bot.py – FINAL Simplified Version
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
# NOTE: aiohttp import is no longer needed unless used elsewhere
# from aiohttp import web # Removed as keep_alive route is gone

# ---------- ENVIRONMENT ----------
REQ = [
    "TELEGRAM_BOT_TOKEN", "SUPABASE_URL", "SUPABASE_KEY",
    "RESEND_API_KEY", "ADMIN_EMAIL", "WEBHOOK_SECRET",
    "RENDER_EXTERNAL_URL"
]
missing = [v for v in REQ if not os.getenv(v)]
if missing:
    print("Missing env vars:", ", ".join(missing)); sys.exit(1)

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
    def emit(self, record):
        try:
            log_entry = json.dumps({
                "t": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "lvl": record.levelname,
                "msg": record.getMessage(),
                "mod": record.name,
            })
            self.stream.write(log_entry + "\n")
            self.flush() # Ensure logs are written out
        except Exception:
            self.handleError(record)


root = logging.getLogger()
root.setLevel(logging.INFO)
# Remove default handlers to avoid duplicate logs if any were added
root.handlers.clear()
root.addHandler(JsonHandler())
# Silence excessive logging from underlying libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("supabase").setLevel(logging.WARNING) # Or INFO if needed
logging.getLogger("telegram.ext").setLevel(logging.INFO) # Can be noisy, adjust if needed

logger = logging.getLogger("bot") # Use this for bot-specific logs

# ---------- CLIENTS ----------
supabase: Optional[Client] = None
try:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("Supabase client initialized.")
except Exception as e:
    logger.error(f"Failed to initialize Supabase client: {e}", exc_info=True)
    # Decide if you want to exit or try to run without Supabase
    # sys.exit(1) # Uncomment to exit if Supabase is critical

try:
    resend.api_key = RESEND_KEY
    logger.info("Resend API key configured.")
except Exception as e:
    logger.error(f"Failed to configure Resend client: {e}", exc_info=True)
    # Resend might not be critical, so we might not exit

# ---------- STATES ----------
ASK_VIN_KNOWN, GET_VIN, GET_CONTACT, GET_PARTS = range(4)

# ---------- UTILITIES ----------
async def _run_sync_in_thread(func, *args, **kwargs):
    """Runs a synchronous function in a separate thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

async def _insert_async(table: str, data: Dict[str, Any]) -> None:
    """Non-blocking Supabase insert."""
    if not supabase:
        logger.error("Supabase client not available for insert.")
        raise ConnectionError("Supabase client not initialized")
    # Supabase client's execute() is blocking, run it in a thread
    await _run_sync_in_thread(
        supabase.table(table).insert(data, returning="minimal").execute # Changed returning to minimal
    )

async def log_interaction(
    update: Optional[Update],
    context: ContextTypes.DEFAULT_TYPE,
    interaction_type: str,
    detail: Optional[str] = None,
    user_id_override: Optional[int] = None,
):
    user_id = username = first_name = None
    if update and update.effective_user:
        u = update.effective_user
        user_id, username, first_name = u.id, u.username, u.first_name
    elif user_id_override:
        user_id = user_id_override

    if not user_id:
        logger.warning("No user information for interaction log.")
        return

    payload = {
        k: v
        for k, v in {
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "interaction_type": interaction_type,
            "interaction_detail": detail,
            # Add timestamp if your table needs it, Supabase adds 'created_at' by default
            # "timestamp": datetime.utcnow().isoformat()
        }.items()
        if v is not None
    }

    # Schedule the background task correctly
    try:
        task = context.application.create_task(
            _insert_async("bot_usage_log", payload),
            update=update, # Pass update for context if needed by task error handling
            name=f"log_interaction_{user_id}_{interaction_type}" # Optional: name the task
        )
        # Add a callback to log if the background task fails
        task.add_done_callback(_handle_task_result)
    except Exception as e:
         logger.error(f"Failed to schedule log_interaction task: {e}", exc_info=True)


async def send_admin_notification(user: dict, order: dict):
    if not RESEND_KEY or not ADMIN_EMAIL:
         logger.error("Resend not configured, skipping admin email.")
         return False
    html = f"""
    <h2>Получен новый запрос</h2><hr>
    <p><b>ID:</b> {user['id']}</p>
    <p><b>User:</b> @{user.get('username','нет')}</p>
    <p><b>VIN:</b> {order.get('vin','не указан')}</p>
    <p><b>Контакт:</b> {order.get('contact','нет')}</p><hr>
    <p><b>Детали:</b></p><blockquote>{order['parts']}</blockquote><hr>
    """
    params = {
            "from": "Parts Bot <bot@asiatek.pro>",
            "to": [ADMIN_EMAIL],
            "subject": "Новый запрос на автозапчасти",
            "html": html,
        }
    try:
        # resend.Emails.send is blocking, run it in a thread
        email_response = await _run_sync_in_thread(resend.Emails.send, params)
        logger.info(f"Admin notification sent. ID: {email_response.get('id')}")
        return True
    except Exception as e:
        logger.error(f"Failed to send admin notification via Resend: {e}", exc_info=True)
        return False


async def save_order_to_supabase(**data) -> bool:
    """Saves order, ensuring required fields are present."""
    # Basic validation before attempting insert
    if not all(data.get(k) for k in ["telegram_user_id", "contact", "parts"]):
        logger.error(f"Missing critical order data before save: {data}")
        return False

    # Remove None values before insert
    data_to_insert = {k: v for k, v in data.items() if v is not None}

    try:
        await _insert_async("orders", data_to_insert)
        logger.info(f"Order saved for user {data.get('telegram_user_id')}")
        return True
    except Exception as e:
        logger.error(f"Supabase order insert failed for user {data.get('telegram_user_id')}: {e}", exc_info=True)
        return False

# ---------- HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.effective_chat:
        logger.warning("Start command received without user or chat info.")
        return ConversationHandler.END # Cannot proceed

    u, chat = update.effective_user, update.effective_chat
    is_restart = update.callback_query is not None
    log_detail = "new_request_button" if is_restart else "/start"

    await log_interaction(update, context, "command", log_detail)
    logger.info(f"User {u.id} ({u.username}) started conversation via {log_detail}.")

    context.user_data.clear()
    context.user_data.update({"id": u.id, "username": u.username, "vin": None})

    text = f"👋 Привет, {u.mention_html()}!\nЗнаете ли вы VIN?"
    reply_markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Да", callback_data="vin_yes")],
            [InlineKeyboardButton("❌ Нет", callback_data="vin_no")],
        ]
    )

    if is_restart:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text, parse_mode=constants.ParseMode.HTML, reply_markup=reply_markup
        )
    else:
        await chat.send_message(
            text, parse_mode=constants.ParseMode.HTML, reply_markup=reply_markup
        )

    return ASK_VIN_KNOWN

async def ask_vin_known_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if not query or not query.from_user: return ConversationHandler.END # Should not happen
    await query.answer()
    await log_interaction(update, context, "callback_query", query.data)
    logger.info(f"User {query.from_user.id} answered VIN known: {query.data}")

    if query.data == "vin_yes":
        await query.edit_message_text("Введите 17‑значный VIN:"); return GET_VIN
    elif query.data == "vin_no":
        # If VIN is no, ask for contact directly
        context.user_data["vin"] = None # Explicitly set VIN to None
        await query.edit_message_text("Укажите телефон или e‑mail:"); return GET_CONTACT
    else:
        # Should not happen with the defined buttons
        logger.warning(f"Unexpected callback data in ask_vin_known: {query.data}")
        await query.edit_message_text("Ошибка. Нажмите /start.")
        return ConversationHandler.END

async def get_vin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text or not update.effective_user:
        # Handle cases where message might be missing (e.g., user sends sticker)
        if update.effective_message:
            await update.effective_message.reply_text("Пожалуйста, введите текст VIN или /cancel.")
        return GET_VIN # Stay in the same state

    vin = update.message.text.strip().upper()
    user_id = update.effective_user.id

    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        logger.warning(f"User {user_id} provided invalid VIN: {vin}")
        await update.message.reply_text("Неверный VIN (17 символов, латиница и цифры). Попробуйте ещё или /cancel."); return GET_VIN

    context.user_data["vin"] = vin
    await log_interaction(update, context, "step_complete", "vin_provided")
    logger.info(f"User {user_id} provided valid VIN.")
    await update.message.reply_text("Спасибо! Теперь контакт (телефон или e‑mail):"); return GET_CONTACT


async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text or not update.effective_user:
        if update.effective_message:
            await update.effective_message.reply_text("Пожалуйста, введите текст контакта или /cancel.")
        return GET_CONTACT

    contact = update.message.text.strip()
    user_id = update.effective_user.id

    # Basic contact validation (adjust regex/length as needed)
    if len(contact) < 5: # Simple length check
        logger.warning(f"User {user_id} provided short contact: {contact}")
        await update.message.reply_text("Пожалуйста, введите корректный телефон/e‑mail или /cancel."); return GET_CONTACT

    context.user_data["contact"] = contact
    await log_interaction(update, context, "step_complete", "contact_provided")
    logger.info(f"User {user_id} provided contact.")
    await update.message.reply_text("Отлично! Опишите нужные запчасти:"); return GET_PARTS

async def get_parts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text or not update.effective_user:
        if update.effective_message:
            await update.effective_message.reply_text("Пожалуйста, опишите запчасти или /cancel.")
        return GET_PARTS

    parts = update.message.text.strip()
    user_id = update.effective_user.id

    if not parts:
         await update.message.reply_text("Описание запчастей не может быть пустым. /cancel для отмены"); return GET_PARTS

    d = context.user_data
    # Ensure critical data exists from previous steps
    if "id" not in d or "contact" not in d:
         logger.error(f"Critical data missing in get_parts for user {user_id}. Context: {d}")
         await update.message.reply_text("Произошла ошибка с вашими данными. Нажмите /start.")
         context.user_data.clear(); return ConversationHandler.END

    await log_interaction(update, context, "step_complete", "parts_provided")
    logger.info(f"User {user_id} described parts. Preparing to save.")

    # Prepare data for saving
    order_data = {
        "telegram_user_id": d["id"],
        "telegram_username": d.get("username"), # Username might be None
        "vin": d.get("vin"), # VIN might be None
        "contact_info": d["contact"],
        "parts_needed": parts,
    }

    save_ok = await save_order_to_supabase(**order_data)

    if save_ok:
        logger.info(f"Order successfully saved for user {user_id}.")
        await log_interaction(update, context, "action_complete", "order_saved")
        # Try sending notification, but don't block user if it fails
        context.application.create_task(
             send_admin_notification(
                 {"id": d["id"], "username": d.get("username")},
                 {"vin": d.get("vin"), "contact": d["contact"], "parts": parts},
             ),
             update=update,
             name=f"send_admin_notification_{user_id}"
        ).add_done_callback(_handle_task_result) # Log notification task result
        msg = "✅ Запрос сохранён и отправлен! Мы скоро свяжемся с вами."
    else:
        logger.error(f"Failed to save order for user {user_id}.")
        await log_interaction(update, context, "action_failed", "order_save_failed")
        msg = "❌ Произошла ошибка при сохранении вашего запроса. Пожалуйста, попробуйте позже или свяжитесь с администратором."

    # Always offer to start again
    await update.message.reply_text(
        msg,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("➕ Запросить снова", callback_data="new_request")]]
        ),
    )
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id if update.effective_user else "Unknown"
    await log_interaction(update, context, "command", "/cancel")
    logger.info(f"User {user_id} cancelled the conversation.")
    if update.effective_message:
        await update.effective_message.reply_text(
            "🚫 Запрос отменен.", reply_markup=ReplyKeyboardRemove()
        )
    context.user_data.clear()
    return ConversationHandler.END

async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles messages outside the conversation flow or unexpected commands."""
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
    # You might choose to ignore it silently or just let the standard reply happen.
    # For simplicity, we let the standard reply happen, which confirms it was received.
    # if text == "ping":
    #     logger.info("Keep-alive ping received via fallback handler.")
    #     # Optionally return here if you want no reply to the ping
    #     # return

    # Provide a helpful response to the user
    if text.startswith('/'):
        reply_text = f"Команда {text} сейчас не ожидается. Используйте /cancel для отмены или /start для начала."
    else:
        # Generic response for unexpected text/media
        reply_text = "Извините, я этого не ожидал. Если вы в процессе запроса, следуйте инструкциям. Нажмите /start, чтобы начать сначала, или /cancel для отмены."

    await update.effective_message.reply_text(reply_text)


# ---------- ERROR HANDLING ----------
def _handle_task_result(task: asyncio.Task) -> None:
    """Log exceptions from background tasks."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass  # Task cancellation should not be logged as an error.
    except Exception:
        logger.exception(f"Exception raised by background task '{task.get_name()}':")


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log Errors caused by Updates."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    # Optionally send a message to the user or admin
    # if isinstance(update, Update) and update.effective_message:
    #     await update.effective_message.reply_text("Произошла ошибка. Попробуйте позже.")


# ---------- MAIN ----------
def main() -> None:
    """Start the bot."""
    logger.info("Starting bot application...")
    # NOTE: Removed .post_init() from builder - it was causing issues.
    application = (
        Application.builder()
        .token(TG_TOKEN)
        .build()
    )

    # Register the global error handler
    application.add_error_handler(global_error_handler)

    # Conversation Handler Setup
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(start, pattern="^new_request$") # Allows restarting via button
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
            # Fallback handler for any unexpected command or message within the conversation
            MessageHandler(filters.COMMAND, fallback_handler),
            MessageHandler(filters.ALL, fallback_handler)
        ],
        name="car_parts_conversation",
        persistent=False # Keep state in memory, not disk
    )
    application.add_handler(conv_handler)

    # Add a fallback handler outside the conversation for messages that don't start it
    application.add_handler(MessageHandler(filters.COMMAND | filters.ALL, fallback_handler))

    # --- Webhook Setup ---
    webhook_path = "/webhook" # Consistent path
    full_webhook_url = f"{BASE_URL}{webhook_path}"
    logger.info(f"Setting webhook URL: {full_webhook_url}")
    logger.info(f"Webhook server listening on port {PORT} for path {webhook_path}")

    # Run the webhook server
    # NOTE: Removed post_init from here as it's invalid syntax for run_webhook
    # NOTE: Kept secret_token for security!
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_path,
        webhook_url=full_webhook_url,
        secret_token=WEBHOOK_SECRET,
    )
    logger.info("Webhook server stopped.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Catch critical startup errors not handled elsewhere
        logger.critical(f"Application failed to start: {e}", exc_info=True)
        sys.exit(1)
