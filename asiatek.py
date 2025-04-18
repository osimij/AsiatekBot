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
# Ensure you have 'supabase' (the community Supabase Python library) installed, NOT 'supabase-py'
# Make sure requirements.txt has 'supabase' version 2.15.0 (or newer compatible with p-t-b 21.7+)
from supabase import create_client, Client
import resend

# --- Configuration (Fetched from Environment Variables) ---
# These variables MUST be set in your Render service environment config.
# We are removing hardcoded values here for security and flexibility.
# Use os.environ.get() to safely retrieve variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

# Render provides these environment variables automatically when running a web service
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", 8080)) # Default to 8080 if PORT not set by Render (unlikely for web)

# --- Basic Configuration Check ---
# Perform this check early before client initialization
required_vars = ["TELEGRAM_BOT_TOKEN", "SUPABASE_URL", "SUPABASE_KEY", "RESEND_API_KEY", "ADMIN_EMAIL", "WEBHOOK_SECRET", "RENDER_EXTERNAL_URL"]
missing_vars = [var_name for var_name in required_vars if os.environ.get(var_name) is None]
if missing_vars:
    logging.critical(f"Missing required environment variables: {', '.join(missing_vars)}. Bot cannot start.")
    # Use sys.exit(1) to ensure the process terminates correctly on Render if config is missing
    sys.exit(1)

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Set logging level for potentially noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("supabase").setLevel(logging.INFO) # Set supabase logging level as needed
logger = logging.getLogger(__name__) # Get a logger for your script


# --- Initialize Clients ---
supabase: Client | None = None
try:
    # Ensure client is created only if URL and KEY are present (checked above)
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY) # type: ignore # Ignore type checking if variables can be None
    logger.info("Supabase client initialized successfully.")
except Exception as e:
    logger.error(f"CRITICAL: Failed to initialize Supabase client: {e}")
    # If Supabase client initialization is critical for your bot's basic function, exit.
    sys.exit(1)

try:
    # Ensure API key is present (checked above)
    resend.api_key = RESEND_API_KEY # type: ignore # Ignore type checking if variable can be None
    logger.info("Resend client configured successfully.")
except Exception as e:
    logger.error(f"CRITICAL: Failed to configure Resend client: {e}")
    # If Resend is not critical for *initial* bot startup, you might not exit here,
    # but handle email sending failures gracefully later (which send_admin_notification does).
    # For now, we'll just log the critical error during startup.
    pass


# --- Conversation States ---
ASK_VIN_KNOWN, GET_VIN, GET_CONTACT, GET_PARTS_VIN, GET_PARTS_CONTACT = range(5)

# --- Helper Functions ---

async def send_admin_notification(user_details: dict, order_details: dict):
    """Sends an email notification to the admin using Resend."""
    # Re-check API key and admin email just in case (redundant if checked on startup, but safe)
    if not RESEND_API_KEY or not ADMIN_EMAIL:
        logger.error("Resend API Key or Admin Email is not configured for sending notification.")
        return False

    # Attempt to use a verified domain from your Resend account for the 'from' address.
    # Replace "bot@yourdomain.com" with your actual verified domain email.
    # Using the hardcoded one "bot@asiatek.pro" based on previous recap, but ideally
    # this should be a domain you control and verify with Resend.
    # Ensure this 'from' address is verified in your Resend account!
    from_address = "Parts Bot <bot@asiatek.pro>" # !!! IMPORTANT: CHANGE IF YOUR VERIFIED DOMAIN IS DIFFERENT !!!

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
            "from": from_address,
            "to": [ADMIN_EMAIL], # Should be asiatek.pro@outlook.com
            "subject": subject,
            "html": html_body,
        }
        email = resend.Emails.send(params)
        logger.info(f"Admin notification email sent successfully via Resend: {email.get('id', 'N/A')}") # Use .get for safety
        return True
    except Exception as e:
        logger.error(f"Failed to send admin notification email via Resend. Error: {e}")
        # Log the attempted params *without* revealing API key or sensitive user data if possible
        logger.error(f"Resend params attempted: From={params.get('from')}, To={params.get('to')}, Subject={params.get('subject')}")
        return False

# --- MODIFIED save_order_to_supabase function (using returning=None fix) ---
async def save_order_to_supabase(user_id: int, username: str | None, parts: str, vin: str | None = None, contact: str | None = None) -> bool:
    """Saves the order details to the Supabase 'orders' table."""
    # Re-check if supabase client was initialized successfully
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
    # Only include non-None values in the insert data
    data_to_insert = {k: v for k, v in data.items() if v is not None}

    logger.info(f"Attempting to insert data into '{target_table}': {data_to_insert}")

    try:
        # Execute the insert. With returning=None, this call succeeds if the insert
        # is accepted by PostgREST, without waiting for the row data back.
        # The .execute() method itself will raise an exception on error.
        supabase.table(target_table).insert(data_to_insert, returning=None).execute()

        logger.info(f"Supabase insert command executed successfully for user {user_id}.")
        # If .execute() didn't raise an exception, we assume success.
        return True

    except Exception as e:
        logger.error(f"Failed to save order to Supabase table '{target_table}' for user {user_id}. Data attempted: {data_to_insert}")
        # Attempt to extract more specific Supabase/PostgREST error details
        error_message = f"General exception: {e}"
        if hasattr(e, 'message'): error_message += f" | Message: {e.message}"
        if hasattr(e, 'code'): error_message += f" | Code: {e.code}"
        if hasattr(e, 'details'): error_message += f" | Details: {e.details}"
        if hasattr(e, 'hint'): error_message += f" | Hint: {e.hint}"
        logger.error(f"Supabase error details: {error_message}")
        return False

# --- Command and Conversation Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation and asks if the user knows their VIN."""
    user = update.effective_user
    if not user:
        logger.warning("Received /start command from user object is None.")
        return ConversationHandler.END

    logger.info(f"User {user.id} ({user.username or 'NoUsername'}) started the bot.")

    await update.message.reply_html(
        f"👋 Welcome, {user.mention_html()}!\n\n"
        "I can help you request car parts. To get started, please tell me:",
    )

    # Store user details in conversation context
    context.user_data['telegram_user_id'] = user.id
    context.user_data['telegram_username'] = user.username

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
    if not query:
        logger.warning("ask_vin_known_handler received update without callback_query.")
        # Decide how to handle this - maybe inform user to restart or re-send buttons
        if update.effective_message:
             await update.effective_message.reply_text("Sorry, I didn't get that. Please use the buttons, or /start again.")
             # Could potentially redisplay buttons here
        return ASK_VIN_KNOWN # Stay in state

    await query.answer() # Acknowledge the button press
    user_choice = query.data
    user = query.from_user # Get the user who clicked the button

    # Ensure user data is present if not already from /start (e.g., if starting convo with callback)
    context.user_data.setdefault('telegram_user_id', user.id)
    context.user_data.setdefault('telegram_username', user.username)

    if user_choice == "vin_yes":
        logger.info(f"User {user.id} chose 'Yes' to VIN.")
        await query.edit_message_text(text="Great! Please enter your 17-digit VIN.")
        return GET_VIN # Proceed to get VIN state
    elif user_choice == "vin_no":
        logger.info(f"User {user.id} chose 'No' to VIN.")
        await query.edit_message_text(
            text="No problem. Please provide your phone number or email address so we can contact you."
        )
        return GET_CONTACT # Proceed to get contact state
    else:
        logger.warning(f"User {user.id} sent unexpected callback data: {user_choice}")
        await query.edit_message_text(text="Sorry, I didn't understand that. Please use the buttons.")
        # Re-send buttons for clarity
        keyboard = [
            [InlineKeyboardButton("✅ Yes, I know my VIN", callback_data="vin_yes")],
            [InlineKeyboardButton("❌ No, I don't know my VIN", callback_data="vin_no")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        # Send as a new message after editing the old one
        await query.message.reply_text("Do you know your car's VIN?", reply_markup=reply_markup)
        return ASK_VIN_KNOWN # Stay in the same state

async def get_vin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores the VIN and asks for the parts needed."""
    user = update.effective_user
    if not user or not update.message or not update.message.text:
        logger.warning("get_vin received invalid update (no user or text message).")
        return GET_VIN # Stay in state

    user_vin = update.message.text.strip() # Remove leading/trailing whitespace
    logger.info(f"User {user.id} attempting to provide VIN: {user_vin}")

    # Basic VIN validation (17 alphanumeric chars, excluding I, O, Q)
    if not re.match(r"^[A-HJ-NPR-Z0-9]{17}$", user_vin.upper()):
         logger.warning(f"User {user.id} provided invalid VIN format: {user_vin}")
         await update.message.reply_text(
             "That doesn't look like a valid 17-character VIN (only letters A-Z except I,O,Q and numbers 0-9).\nPlease try again, or type /cancel to stop."
         )
         return GET_VIN # Stay in this state

    context.user_data['vin'] = user_vin.upper() # Store VIN, ensure uppercase
    logger.info(f"User {user.id} successfully provided VIN: {context.user_data['vin']}")

    await update.message.reply_text(
        "Thank you! Now, please describe the car parts or items you need.",
         reply_markup=ReplyKeyboardRemove(), # Remove any previous custom keyboard
    )
    return GET_PARTS_VIN # Next state: get parts description

async def get_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores the contact info and asks for the parts needed."""
    user = update.effective_user
    if not user or not update.message or not update.message.text:
        logger.warning("get_contact received invalid update (no user or text message).")
        return GET_CONTACT # Stay in state

    user_contact = update.message.text.strip()
    logger.info(f"User {user.id} attempting to provide contact: {user_contact}")

    # Simple check for contact length
    if len(user_contact) < 5:
         logger.warning(f"User {user.id} provided short contact info: {user_contact}")
         await update.message.reply_text(
            "Please enter a valid phone number or email address (at least 5 characters).\nOr type /cancel to stop."
            )
         return GET_CONTACT # Stay in this state

    context.user_data['contact'] = user_contact # Store contact info
    logger.info(f"User {user.id} successfully provided contact info.")

    await update.message.reply_text(
        "Got it! Now, please describe the car parts or items you need.",
         reply_markup=ReplyKeyboardRemove(),
    )
    return GET_PARTS_CONTACT # Next state: get parts description

async def get_parts_vin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets parts description (after VIN), saves order, notifies admin, and ends conversation."""
    user = update.effective_user
    if not user or not update.message or not update.message.text:
        logger.warning("get_parts_vin received invalid update (no user or text message).")
        context.user_data.clear() # Clear data as flow is broken
        return ConversationHandler.END # End conversation

    parts_needed = update.message.text.strip()
    if not parts_needed:
        await update.message.reply_text("Please describe the parts you need, or type /cancel.")
        return GET_PARTS_VIN # Stay in state if empty message

    # Retrieve stored user data from context.user_data
    user_id = context.user_data.get('telegram_user_id')
    username = context.user_data.get('telegram_username')
    vin = context.user_data.get('vin')

    # Critical check: Ensure necessary data is present
    if user_id is None or vin is None:
         logger.error(f"Error: User data (ID:{user_id}, VIN:{vin}) missing in get_parts_vin context.")
         await update.message.reply_text("Sorry, something went wrong with retrieving your details. Please /start again.")
         context.user_data.clear() # Clear potentially incomplete data
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

    context.user_data.clear() # Clear conversation data after completion
    return ConversationHandler.END # End the conversation

async def get_parts_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets parts description (after Contact), saves order, notifies admin, and ends conversation."""
    user = update.effective_user
    if not user or not update.message or not update.message.text:
        logger.warning("get_parts_contact received invalid update (no user or text message).")
        context.user_data.clear() # Clear data as flow is broken
        return ConversationHandler.END # End conversation

    parts_needed = update.message.text.strip()
    if not parts_needed:
        await update.message.reply_text("Please describe the parts you need, or type /cancel.")
        return GET_PARTS_CONTACT # Stay in state if empty message

    # Retrieve stored user data from context.user_data
    user_id = context.user_data.get('telegram_user_id')
    username = context.user_data.get('telegram_username')
    contact = context.user_data.get('contact')

    # Critical check: Ensure necessary data is present
    if user_id is None or contact is None:
         logger.error(f"Error: User data (ID:{user_id}, Contact:{contact}) missing in get_parts_contact context.")
         await update.message.reply_text("Sorry, something went wrong with retrieving your details. Please /start again.")
         context.user_data.clear() # Clear potentially incomplete data
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

    context.user_data.clear() # Clear conversation data after completion
    return ConversationHandler.END # End the conversation

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    user = update.effective_user
    # Check if update or effective_message is None before replying
    if update and update.effective_message:
        logger.info(f"User {user.id if user else 'Unknown'} canceled the conversation.")
        await update.effective_message.reply_text(
            "Okay, the request process has been cancelled.", reply_markup=ReplyKeyboardRemove()
        )
    else:
         logger.warning(f"Cancel received invalid update (no user or message). User: {user.id if user else 'Unknown'}")
         # No reply possible if no message to reply to

    context.user_data.clear() # Clear conversation data
    return ConversationHandler.END

async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles messages that are not part of the expected conversation flow."""
    user_id = update.effective_user.id if update.effective_user else "Unknown"
    text = update.message.text if update.message else "[No message text]"
    logger.warning(f"Fallback handler triggered for user {user_id}. Message: '{text}'. State: {context.conversation_state if hasattr(context, 'conversation_state') else 'N/A'}")
    # Only reply if there is an effective message to reply to
    if update and update.effective_message:
         await update.effective_message.reply_text(
            "Sorry, I wasn't expecting that. If you were in the middle of a request, please follow the prompts. "
            "You can always start over with /start or cancel with /cancel."
         )


# --- Main Bot Execution (MODIFIED for Webhooks on Render) ---
def main() -> None:
    """Start the bot using webhooks."""

    # --- Configuration check is already done at the top ---
    # --- Client initialization is already done at the top ---

    logger.info("Initializing Telegram Bot Application for Webhooks...")
    # Configure the Application for webhook mode
    # url_path is where telegram will send updates (e.g., https://your-service.render.com/webhook)
    webhook_url_path = "/webhook"
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).base_url(f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/').build() # type: ignore # Ignore type warning for base_url if needed


    # --- Add Handlers ---
    # PTBUserWarning about per_message=False on CallbackQueryHandler is expected and harmless here.
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            # Ensure callback pattern matches the data sent by buttons
            ASK_VIN_KNOWN: [CallbackQueryHandler(ask_vin_known_handler, pattern="^vin_yes|vin_no$")],
            GET_VIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_vin)],
            GET_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_contact)],
            GET_PARTS_VIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_parts_vin)],
            GET_PARTS_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_parts_contact)],
        },
        fallbacks=[
             CommandHandler("cancel", cancel),
             # Fallback for any text/command not handled by current state
             MessageHandler(filters.ALL & ~filters.COMMAND, fallback_handler), # Handle text not in conv flow
             MessageHandler(filters.COMMAND, fallback_handler) # Handle commands not in conv flow or entry points
        ],
        name="car_parts_conversation",
        persistent=False # Set to True if you implement persistence across restarts
        # Add conversation_timeout=... if you want conversations to expire
    )

    application.add_handler(conv_handler)

    # Add handlers for commands that should work outside the conversation like /start, /cancel
    # (These are already covered by entry_points and fallbacks in the ConversationHandler,
    # but explicitly adding them here ensures they work even if user data is messed up,
    # and prevents them from hitting the generic fallback if the conv_handler fails somehow)
    # application.add_handler(CommandHandler("start", start)) # Already covered by entry_points
    # application.add_handler(CommandHandler("cancel", cancel)) # Already covered by fallbacks

    # Generic handler for any message type if not handled by conversation or explicit commands
    # Place this *after* other handlers so they get priority.
    # application.add_handler(MessageHandler(filters.ALL, fallback_handler)) # Handled by conv_handler fallback

    # --- Configure and Run Webhook ---
    # The webhook_url is already set during Application.builder()
    # Use this line for logging what PTB is configured to send to Telegram


    # --- Removed manual set_webhook call. run_webhook handles it. ---
    # logger.info(f"Setting webhook URL to: {full_webhook_url}")
    # try:
    #     application.bot.set_webhook(url=full_webhook_url, secret_token=WEBHOOK_SECRET, allowed_updates=Update.ALL_TYPES)
    #     logger.info("Telegram webhook set successfully.")
    # except Exception as e:
    #     logger.error(f"Failed to programmatically set Telegram webhook: {e}")
    #     # Don't exit here, hope the curl call worked or run_webhook handles it

    logger.info(f"Starting webhook server on port {PORT} listening for path {webhook_url_path}...")
    # Start the webhook server. This makes your application listen for
    # incoming POST requests from Telegram on the assigned Render port.
    # run_webhook will internally set the webhook with Telegram using application.webhook_url
    application.run_webhook(
        listen="0.0.0.0", # Listen on all network interfaces inside the container
        port=PORT,         # Use the port assigned by Render (fetched from env)
        url_path=webhook_url_path, # The specific path Telegram sends updates to (e.g., "/webhook")
        secret_token=WEBHOOK_SECRET, # Provide the secret token here for run_webhook
        # drop_pending_updates=True # Uncomment if you want to ignore any updates
                                    # Telegram tried to send while the bot was down/misconfigured.
    )
    logger.info("Webhook server stopped.")


if __name__ == "__main__":
    main()