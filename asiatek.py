# bot.py – fully self‑contained, stable build
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
from aiohttp import web

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
        log_entry = json.dumps({
            "t": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "lvl": record.levelname,
            "msg": record.getMessage(),
            "mod": record.name,
        })
        self.stream.write(log_entry + "\n")

root = logging.getLogger()
root.setLevel(logging.INFO)
root.handlers = [JsonHandler()]
logger = logging.getLogger("bot")

# ---------- CLIENTS ----------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
resend.api_key = RESEND_KEY

# ---------- STATES ----------
ASK_VIN_KNOWN, GET_VIN, GET_CONTACT, GET_PARTS = range(4)

# ---------- UTILITIES ----------
async def _insert_async(table: str, data: Dict[str, Any]) -> None:
    await asyncio.to_thread(
        supabase.table(table).insert(data, returning=None).execute
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
    context.application.create_task(_insert_async("bot_usage_log", payload))

async def send_admin_notification(user: dict, order: dict):
    html = f"""
    <h2>Получен новый запрос</h2><hr>
    <p><b>ID:</b> {user['id']}</p>
    <p><b>User:</b> @{user.get('username','нет')}</p>
    <p><b>VIN:</b> {order.get('vin','не указан')}</p>
    <p><b>Контакт:</b> {order.get('contact','нет')}</p><hr>
    <p><b>Детали:</b></p><blockquote>{order['parts']}</blockquote><hr>
    """
    await asyncio.to_thread(
        resend.Emails.send,
        {
            "from": "Parts Bot <bot@asiatek.pro>",
            "to": [ADMIN_EMAIL],
            "subject": "Новый запрос на автозапчасти",
            "html": html,
        },
    )

async def save_order_to_supabase(**data) -> bool:
    try:
        await _insert_async(
            "orders", {k: v for k, v in data.items() if v is not None}
        )
        return True
    except Exception as e:
        logger.error(f"Supabase insert failed: {e}")
        return False

# ---------- HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    u, chat = update.effective_user, update.effective_chat
    await log_interaction(update, context, "command", "/start")
    context.user_data.clear()
    context.user_data.update({"id": u.id, "username": u.username, "vin": None})
    await chat.send_message(
        f"👋 Привет, {u.mention_html()}!\nЗнаете ли вы VIN?",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Да", callback_data="vin_yes")],
                [InlineKeyboardButton("❌ Нет", callback_data="vin_no")],
            ]
        ),
    )
    return ASK_VIN_KNOWN

async def ask_vin_known_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    q = update.callback_query
    await q.answer()
    if q.data == "vin_yes":
        await q.edit_message_text("Введите 17‑значный VIN:"); return GET_VIN
    await q.edit_message_text("Укажите телефон или e‑mail:"); return GET_CONTACT

async def get_vin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    vin = update.message.text.strip().upper()
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        await update.message.reply_text("Неверный VIN, попробуйте ещё."); return GET_VIN
    context.user_data["vin"] = vin
    await update.message.reply_text("Спасибо! Теперь контакт:"); return GET_CONTACT

async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    contact = update.message.text.strip()
    if len(contact) < 5:
        await update.message.reply_text("Введите корректный телефон/e‑mail."); return GET_CONTACT
    context.user_data["contact"] = contact
    await update.message.reply_text("Опишите нужные запчасти:"); return GET_PARTS

async def get_parts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parts = update.message.text.strip()
    d = context.user_data
    ok = await save_order_to_supabase(
        telegram_user_id=d["id"],
        telegram_username=d["username"],
        vin=d.get("vin"),
        contact=d["contact"],
        parts=parts,
    )
    if ok:
        await send_admin_notification(
            {"id": d["id"], "username": d["username"]},
            {"vin": d.get("vin"), "contact": d["contact"], "parts": parts},
        )
        msg = "✅ Запрос сохранён. Мы свяжемся с вами!"
    else:
        msg = "❌ Ошибка сохранения. Попробуйте позже."
    await update.message.reply_text(
        msg,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("➕ Запросить снова", callback_data="new_request")]]
        ),
    )
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await log_interaction(update, context, "command", "/cancel")
    await update.effective_message.reply_text(
        "🚫 Отменено.", reply_markup=ReplyKeyboardRemove()
    )
    context.user_data.clear()
    return ConversationHandler.END

async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await log_interaction(
        update, context, "fallback", (update.message.text or "")[:100]
    )
    await update.effective_message.reply_text("Не понял. /start чтобы начать заново.")

# ---------- KEEP‑ALIVE ----------
async def keep_alive(request): return web.Response(text="OK")

# ---------- ERROR HOOK ----------
async def err_handler(update, context): logger.error(f"Exception: {context.error}")

# ---------- POST‑INIT ROUTES ----------
async def add_aux_routes(app: Application) -> None:
    while getattr(app, "web_app", None) is None:
        await asyncio.sleep(0.05)
    app.web_app.router.add_get("/keep-alive", keep_alive)
    app.web_app.router.add_get("/healthz",   keep_alive)

# ---------- MAIN ----------
def main() -> None:
    app = (
        Application.builder()
        .token(TG_TOKEN)
        .post_init(add_aux_routes)
        .build()
    )
    app.add_error_handler(err_handler)

    app.add_handler(
        ConversationHandler(
            [CommandHandler("start", start),
             CallbackQueryHandler(start, pattern="^new_request$")],
            {
                ASK_VIN_KNOWN: [
                    CallbackQueryHandler(
                        ask_vin_known_handler, pattern="^vin_yes|vin_no$"
                    )
                ],
                GET_VIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_vin)],
                GET_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_contact)],
                GET_PARTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_parts)],
            },
            [
                CommandHandler("cancel", cancel),
                MessageHandler(filters.COMMAND, fallback),
                MessageHandler(filters.ALL, fallback),
            ],
        )
    )

    full_url = f"{BASE_URL}/webhook"
    logger.info(f"Webhook URL: {full_url}")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="/webhook",
        webhook_url=full_url,
    )

if __name__ == "__main__":
    main()
