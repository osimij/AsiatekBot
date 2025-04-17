import logging
import os
import re # For basic VIN validation

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

# --- ⚠️ HARDCODED CONFIGURATION (Not Recommended for Production) ---
# Make absolutely sure these values are correct!
TELEGRAM_BOT_TOKEN = "7440133107:AAGS4Tin2oIB-KvwlDnffNaeeaJxjBJJLiU"
SUPABASE_URL = "https://cosdgdtxunaigdslrjej.supabase.co"
# This MUST be the 'anon' (public) key from your Supabase project API settings
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNvc2RnZHR4dW5haWdkc2xyamVqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM2Mzg3OTQsImV4cCI6MjA1OTIxNDc5NH0.Kw5sspdKrITC42aFpqj7cS2ampQNqzKwa7dmS1NKPzw"
RESEND_API_KEY = "re_NXRcuV2a_AfqeRqfdTQfrYyrQw7PmNaP6"
ADMIN_EMAIL = "asiatek.pro@outlook.com" # The email address to receive notifications

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING) # Reduce httpx noise
logger = logging.getLogger(__name__)

# --- Initialize Clients ---
supabase: Client | None = None
try:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("Supabase client initialized successfully.")
except Exception as e:
    logger.error(f"CRITICAL: Failed to initialize Supabase client: {e}")
    # exit()

try:
    resend.api_key = RESEND_API_KEY
    logger.info("Resend client configured successfully.")
except Exception as e:
    logger.error(f"CRITICAL: Failed to configure Resend client: {e}")
    # exit()

# --- Conversation States ---
ASK_VIN_KNOWN, GET_VIN, GET_CONTACT, GET_PARTS_VIN, GET_PARTS_CONTACT = range(5)

# --- Helper Functions ---
# --- >>> MODIFIED send_admin_notification function (Updated 'from' address) <<< ---
async def send_admin_notification(user_details: dict, order_details: dict):
    """Sends an email notification to the admin using Resend."""
    if not RESEND_API_KEY or not ADMIN_EMAIL:
        logger.error("Resend API Key or Admin Email is not configured. Skipping notification.")
        return False

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

    try:
        params = {
            # --- >>> UPDATED 'from' address using your verified domain <<< ---
            "from": "Parts Bot <bot@asiatek.pro>", # Using your verified domain
            # --- Keep the 'to' address ---
            "to": [ADMIN_EMAIL], # Should be asiatek.pro@outlook.com
            "subject": subject,
            "html": html_body,
        }
        email = resend.Emails.send(params)
        logger.info(f"Admin notification email sent successfully via Resend: {email['id']}")
        return True
    except Exception as e:
        logger.error(f"Failed to send admin notification email via Resend. Error: {e}")
        logger.error(f"Resend params attempted: From={params.get('from')}, To={params.get('to')}, Subject={params.get('subject')}")
        return False

# --- MODIFIED save_order_to_supabase function (using returning=None fix) ---
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
        # Execute the insert with returning=None to prevent implicit SELECT
        insert_result = supabase.table(target_table).insert(data_to_insert, returning=None).execute()

        # --- !!! MODIFIED SUCCESS CHECK !!! ---
        # If the .execute() call above completes without raising an exception,
        # it means the insert was successful when returning=None.
        logger.info(f"Supabase insert command executed successfully for user {user_id}.")
        return True # Assume success if no exception was thrown

    except Exception as e:
        # Log the error and the data attempted
        logger.error(f"Failed to save order to Supabase table '{target_table}' for user {user_id}. Data attempted: {data_to_insert}")
        # Try to log specific Supabase/PostgREST error if available
        error_message = f"General exception: {e}"
        # Check for Supabase/PostgREST specific error attributes
        if hasattr(e, 'message'): error_message += f" | Message: {e.message}"
        if hasattr(e, 'code'): error_message += f" | Code: {e.code}"
        if hasattr(e, 'details'): error_message += f" | Details: {e.details}"
        if hasattr(e, 'hint'): error_message += f" | Hint: {e.hint}"
        logger.error(f"Supabase error details: {error_message}")
        return False

# --- Command and Conversation Handlers (Unchanged) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation and asks if the user knows their VIN."""
    user = update.effective_user
    logger.info(f"User {user.id} ({user.username or 'NoUsername'}) started the bot.")

    await update.message.reply_html(
        f"👋 Welcome, {user.mention_html()}!\n\n"
        "I can help you request car parts. To get started, please tell me:",
    )

    keyboard = [
        [InlineKeyboardButton("✅ Yes, I know my VIN", callback_data="vin_yes")],
        [InlineKeyboardButton("❌ No, I don't know my VIN", callback_data="vin_no")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text("Do you know your car's VIN (Vehicle Identification Number)?", reply_markup=reply_markup)

    return ASK_VIN_KNOWN # Next state

async def ask_vin_known_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the Yes/No answer about knowing the VIN."""
    query = update.callback_query
    await query.answer() # Acknowledge the button press
    user_choice = query.data
    user = query.from_user # Get the user who clicked the button

    context.user_data['telegram_user_id'] = user.id
    context.user_data['telegram_username'] = user.username

    if user_choice == "vin_yes":
        logger.info(f"User {user.id} knows their VIN.")
        await query.edit_message_text(text="Great! Please enter your 17-digit VIN.")
        return GET_VIN # Proceed to get VIN state
    elif user_choice == "vin_no":
        logger.info(f"User {user.id} does not know their VIN.")
        await query.edit_message_text(
            text="No problem. Please provide your phone number or email address so we can contact you."
        )
        return GET_CONTACT # Proceed to get contact state
    else:
        logger.warning(f"User {user.id} sent unexpected callback data: {user_choice}")
        await query.edit_message_text(text="Sorry, I didn't understand that. Please use the buttons.")
        keyboard = [
            [InlineKeyboardButton("✅ Yes, I know my VIN", callback_data="vin_yes")],
            [InlineKeyboardButton("❌ No, I don't know my VIN", callback_data="vin_no")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("Do you know your car's VIN?", reply_markup=reply_markup) # Send as new message
        return ASK_VIN_KNOWN # Stay in the same state

async def get_vin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores the VIN and asks for the parts needed."""
    user_vin = update.message.text.strip() # Remove leading/trailing whitespace
    user = update.effective_user
    logger.info(f"User {user.id} attempting to provide VIN: {user_vin}")

    if not re.match(r"^[A-HJ-NPR-Z0-9]{17}$", user_vin.upper()):
         logger.warning(f"User {user.id} provided invalid VIN format: {user_vin}")
         await update.message.reply_text(
             "That doesn't look like a valid 17-character VIN (only letters A-Z except I,O,Q and numbers 0-9).\nPlease try again, or type /cancel to stop."
         )
         return GET_VIN # Stay in this state

    context.user_data['vin'] = user_vin.upper() # Store VIN
    logger.info(f"User {user.id} successfully provided VIN: {context.user_data['vin']}")

    await update.message.reply_text(
        "Thank you! Now, please describe the car parts or items you need.",
         reply_markup=ReplyKeyboardRemove(), # Remove any previous custom keyboard
    )
    return GET_PARTS_VIN # Next state: get parts description

async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores the contact info and asks for the parts needed."""
    user_contact = update.message.text.strip()
    user = update.effective_user
    logger.info(f"User {user.id} attempting to provide contact: {user_contact}")

    if len(user_contact) < 5: # Very simple check
         logger.warning(f"User {user.id} provided short contact info: {user_contact}")
         await update.message.reply_text(
            "Please enter a valid phone number or email address.\nOr type /cancel to stop."
            )
         return GET_CONTACT # Stay in this state

    context.user_data['contact'] = user_contact
    logger.info(f"User {user.id} successfully provided contact info.")


    await update.message.reply_text(
        "Got it! Now, please describe the car parts or items you need.",
         reply_markup=ReplyKeyboardRemove(),
    )
    return GET_PARTS_CONTACT # Next state: get parts description

async def get_parts_vin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets parts description (after VIN), saves order, notifies admin, and ends conversation."""
    parts_needed = update.message.text.strip()
    if not parts_needed:
        await update.message.reply_text("Please describe the parts you need, or type /cancel.")
        return GET_PARTS_VIN # Stay in state if empty message

    user_id = context.user_data.get('telegram_user_id')
    username = context.user_data.get('telegram_username')
    vin = context.user_data.get('vin')

    if not all([user_id, vin]):
         logger.error("Error retrieving user data (ID or VIN missing) in get_parts_vin.")
         await update.message.reply_text("Sorry, something went wrong with retrieving your details. Please /start again.")
         context.user_data.clear()
         return ConversationHandler.END

    logger.info(f"User {user_id} (with VIN {vin}) needs parts: {parts_needed}")

    # Call the updated save function
    saved = await save_order_to_supabase(user_id, username, parts_needed, vin=vin)

    if saved:
        user_details = {"id": user_id, "username": username}
        order_details = {"vin": vin, "parts": parts_needed}
        # Attempt to send notification
        notified = await send_admin_notification(user_details, order_details)

        await update.message.reply_text(
            "✅ Thank you! Your request has been submitted.\n"
            "We have your VIN and parts list. We will process it and contact you if needed."
        )
        if not notified: # Check if notification failed
            await update.message.reply_text(
                 "(There may have been an issue notifying the admin via email, but your request *is* saved.)"
            )
    else:
        await update.message.reply_text(
            "❌ Sorry, there was an error saving your request to our database. "
            "Please try again later or contact support directly if the problem persists."
        )

    context.user_data.clear()
    return ConversationHandler.END # End the conversation

async def get_parts_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets parts description (after Contact), saves order, notifies admin, and ends conversation."""
    parts_needed = update.message.text.strip()
    if not parts_needed:
        await update.message.reply_text("Please describe the parts you need, or type /cancel.")
        return GET_PARTS_CONTACT # Stay in state if empty message

    user_id = context.user_data.get('telegram_user_id')
    username = context.user_data.get('telegram_username')
    contact = context.user_data.get('contact')

    if not all([user_id, contact]):
         logger.error("Error retrieving user data (ID or Contact missing) in get_parts_contact.")
         await update.message.reply_text("Sorry, something went wrong with retrieving your details. Please /start again.")
         context.user_data.clear()
         return ConversationHandler.END

    logger.info(f"User {user_id} (with contact {contact}) needs parts: {parts_needed}")

    # Call the updated save function
    saved = await save_order_to_supabase(user_id, username, parts_needed, contact=contact)

    if saved:
        user_details = {"id": user_id, "username": username}
        order_details = {"contact": contact, "parts": parts_needed}
        # Attempt to send notification
        notified = await send_admin_notification(user_details, order_details)

        await update.message.reply_text(
            "✅ Thank you! Your request has been submitted.\n"
            "We have your contact details and parts list. We will contact you soon!"
        )
        if not notified: # Check if notification failed
             await update.message.reply_text(
                 "(There may have been an issue notifying the admin via email, but your request *is* saved.)"
             )
    else:
        await update.message.reply_text(
            "❌ Sorry, there was an error saving your request to our database. "
            "Please try again later or contact support directly if the problem persists."
        )

    context.user_data.clear()
    return ConversationHandler.END # End the conversation

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    user = update.effective_user
    logger.info(f"User {user.id if user else 'Unknown'} canceled the conversation.")
    await update.message.reply_text(
        "Okay, the request process has been cancelled.", reply_markup=ReplyKeyboardRemove()
    )
    context.user_data.clear()
    return ConversationHandler.END

async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles messages that are not part of the expected conversation flow."""
    user_id = update.effective_user.id if update.effective_user else "Unknown"
    text = update.message.text if update.message else "[No message text]"
    logger.warning(f"Fallback handler triggered for user {user_id}. Message: '{text}'. State: {context.conversation_state if hasattr(context, 'conversation_state') else 'N/A'}")
    await update.message.reply_text(
        "Sorry, I wasn't expecting that. If you were in the middle of a request, please follow the prompts. "
        "You can always start over with /start or cancel with /cancel."
        )

# --- Main Bot Execution (Unchanged) ---
def main() -> None:
    """Start the bot."""

    if not all([TELEGRAM_BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY, RESEND_API_KEY, ADMIN_EMAIL]):
        logger.critical("One or more hardcoded configuration variables are empty! Please check the code.")
        return
    if "YOUR_" in TELEGRAM_BOT_TOKEN or "YOUR_" in SUPABASE_URL:
        logger.critical("Configuration variables seem to contain placeholder text! Please check the hardcoded values.")
        return

    if supabase is None:
        logger.critical("Supabase client failed to initialize. Bot cannot function properly. Exiting.")
        return

    logger.info("Initializing Telegram Bot Application...")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_VIN_KNOWN: [CallbackQueryHandler(ask_vin_known_handler, pattern="^(vin_yes|vin_no)$")],
            GET_VIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_vin)],
            GET_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_contact)],
            GET_PARTS_VIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_parts_vin)],
            GET_PARTS_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_parts_contact)],
        },
        fallbacks=[
             CommandHandler("cancel", cancel),
             MessageHandler(filters.COMMAND | filters.TEXT, fallback_handler)
        ],
        name="car_parts_conversation",
    )

    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.COMMAND, fallback_handler))

    logger.info("Bot starting polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Bot polling stopped.")

if __name__ == "__main__":
    main()