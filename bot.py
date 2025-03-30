import logging
import os
import re
import asyncio
import json
from uuid import uuid4

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Telegram Bot imports
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.constants import ChatAction

# Downloader import
import yt_dlp

# --- Configuration ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "./downloads")

# Ensure download directory exists
os.makedirs(DOWNLOAD_PATH, exist_ok=True)

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING) # Reduce httpx verbosity
logger = logging.getLogger(__name__)

# --- Helper Functions ---
def get_url_type(url):
    """Identifies the source platform from the URL."""
    if re.search(r"(youtube\.com|youtu\.be)", url):
        return "youtube"
    elif re.search(r"soundcloud\.com", url):
        return "soundcloud"
    elif re.search(r"tiktok\.com", url):
        return "tiktok"
    else:
        return "unknown"

async def send_typing_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends typing action periodically."""
    while context.user_data.get('processing', False):
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        await asyncio.sleep(4) # Telegram action expires after 5 seconds

async def send_upload_action(update: Update, context: ContextTypes.DEFAULT_TYPE, file_type: str):
    """Sends uploading action periodically."""
    action = ChatAction.UPLOAD_VIDEO if file_type == 'video' else ChatAction.UPLOAD_AUDIO
    while context.user_data.get('uploading', False):
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=action)
        await asyncio.sleep(4)

def cleanup_file(file_path):
    """Removes a file if it exists."""
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
            logger.info(f"Cleaned up file: {file_path}")
        except OSError as e:
            logger.error(f"Error removing file {file_path}: {e}")

# --- yt-dlp Download Logic ---

# Using asyncio.to_thread to run synchronous yt-dlp code in a separate thread
async def run_youtubedl(options: dict):
    """Runs yt-dlp synchronously in a separate thread."""
    loop = asyncio.get_running_loop()
    def download():
        with yt_dlp.YoutubeDL(options) as ydl:
            try:
                # Ensure 'urls' key exists and has the URL
                if 'urls' not in options or not options['urls']:
                     raise ValueError("URL is missing in yt-dlp options")
                logger.debug(f"Starting yt-dlp download with options: {options}")
                return ydl.extract_info(options['urls'][0], download=True)
            except yt_dlp.utils.DownloadError as e:
                logger.error(f"yt-dlp DownloadError: {e}")
                raise  # Re-raise the exception to be caught later
            except Exception as e:
                logger.error(f"yt-dlp generic error: {e}", exc_info=True) # Log full traceback
                raise # Re-raise

    # Run the synchronous function in a thread pool executor
    info_dict = await loop.run_in_executor(None, download)
    return info_dict


async def download_media(url: str, format_options: dict, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Downloads media using yt-dlp with specified format options."""
    # Check if update has effective_chat and effective_message (might be missing in rare cases)
    if not hasattr(update, 'effective_chat') or not update.effective_chat:
         logger.error("download_media called without effective_chat in update")
         return
    if not hasattr(update, 'effective_message') or not update.effective_message:
         logger.error("download_media called without effective_message in update")
         # Try getting chat_id differently if possible, or return
         chat_id = update.effective_chat.id
         message_id = None # Can't reply directly
         status_message = await context.bot.send_message(chat_id=chat_id, text="⏳ Processing link...")
    else:
        chat_id = update.effective_chat.id
        message_id = update.effective_message.message_id
        status_message = await context.bot.send_message(chat_id=chat_id, text="⏳ Processing link...", reply_to_message_id=message_id)


    # Start sending typing action
    context.user_data['processing'] = True
    processing_task = asyncio.create_task(send_typing_action(update, context))

    # Add URL to options
    format_options['urls'] = [url]
    # Define output template - using a unique ID to avoid filename clashes
    output_filename = os.path.join(DOWNLOAD_PATH, f"{uuid4()}.%(ext)s")
    format_options['outtmpl'] = output_filename

    downloaded_file_path = None
    info_dict = None
    error_message = None

    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text="⏳ Downloading...")
        info_dict = await run_youtubedl(format_options)

        # Stop processing action
        context.user_data['processing'] = False
        processing_task.cancel()

        # --- Robust file path finding ---
        actual_filepath = None
        # 1. Check 'requested_downloads' (most reliable for successful downloads)
        if info_dict and 'requested_downloads' in info_dict and info_dict['requested_downloads']:
            actual_filepath = info_dict['requested_downloads'][0].get('filepath')

        # 2. Fallback to '_filename' or 'filepath' at the root (might be set even if download=False)
        if not actual_filepath and info_dict:
            actual_filepath = info_dict.get('filepath') or info_dict.get('_filename')

        # 3. Check if the guessed path from outtmpl exists (last resort)
        # yt-dlp should fill the extension correctly in info_dict if possible
        if not actual_filepath and info_dict and format_options.get('outtmpl'):
             guessed_path = format_options['outtmpl']
             # Try replacing %(ext)s with actual extension if available
             if '%(ext)s' in guessed_path and info_dict.get('ext'):
                 guessed_path = guessed_path.replace('%(ext)s', info_dict['ext'])
             # If still contains template or is the raw template, try finding file by UUID
             if '%(ext)s' in guessed_path or guessed_path == output_filename:
                  uuid_part = os.path.basename(output_filename).split('.')[0]
                  found = False
                  for f in os.listdir(DOWNLOAD_PATH):
                       if f.startswith(uuid_part):
                            actual_filepath = os.path.join(DOWNLOAD_PATH, f)
                            found = True
                            logger.info(f"Found file by UUID match: {actual_filepath}")
                            break
             elif os.path.exists(guessed_path): # Check if the simple template replacement worked
                   actual_filepath = guessed_path

        if not actual_filepath or not os.path.exists(actual_filepath):
            logger.error(f"Could not determine downloaded file path. Template: {output_filename}. Info dict: {json.dumps(info_dict, indent=2)}")
            raise FileNotFoundError(f"Could not locate downloaded file based on template: {output_filename}.")

        downloaded_file_path = actual_filepath # Assign the found path
        logger.info(f"Download complete. Determined file path: {downloaded_file_path}")
        # --- End robust file path finding ---


        # Send the file
        file_type = 'video' if format_options.get('format', '').startswith(('bv', 'bestvideo', 'mp4')) else 'audio'
        await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text=f"⬆️ Uploading {file_type}...")

        # Start uploading action
        context.user_data['uploading'] = True
        upload_task = asyncio.create_task(send_upload_action(update, context, file_type))

        title = info_dict.get('title', 'Downloaded Media')
        duration = info_dict.get('duration')
        # Thumbnail handling can be complex, skipping for simplicity now

        # Send the appropriate file type
        try:
            send_kwargs = {
                'chat_id': chat_id,
                'title': title,
                'duration': duration,
                'reply_to_message_id': message_id,
                'write_timeout': 180 # Increase timeout further for large files/slow connections
            }
            if file_type == 'audio':
                await context.bot.send_audio(audio=open(downloaded_file_path, 'rb'), **send_kwargs)
            else: # video
                width = info_dict.get('width')
                height = info_dict.get('height')
                supports_streaming = True
                send_kwargs.update({
                    'caption': title, # Use caption for video
                    'width': width,
                    'height': height,
                    'supports_streaming': supports_streaming
                })
                # Remove title for video as caption is used
                send_kwargs.pop('title', None)
                await context.bot.send_video(video=open(downloaded_file_path, 'rb'), **send_kwargs)

            await status_message.delete() # Delete "Uploading..." message on success

        except Exception as send_error:
             logger.error(f"Error sending file to Telegram: {send_error}", exc_info=True)
             # Try to make error message more user-friendly
             error_detail = str(send_error)
             if "Request Entity Too Large" in error_detail:
                  error_message = "❌ Error: File is too large for Telegram."
             elif "Timed out" in error_detail:
                   error_message = "❌ Error: Upload timed out. File might be too large or network is slow."
             else:
                   error_message = f"❌ Error sending file. Please try again later." # Generic error for user

             await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text=error_message)
             # Keep internal error message if cleanup fails? No, user message is enough
             # error_message = f"Internal Error sending file: {send_error}"

        finally:
             # Stop uploading action
             context.user_data['uploading'] = False
             if 'upload_task' in locals() and not upload_task.done():
                 upload_task.cancel()


    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Download failed: {e}")
        # Try to extract a meaningful part of the error
        error_parts = str(e).split(':')
        simple_error = error_parts[-1].strip() if len(error_parts) > 1 else str(e)
        if "Unsupported URL" in simple_error:
             error_message = f"❌ Download failed: Unsupported URL or format."
        elif "Unable to extract" in simple_error:
              error_message = f"❌ Download failed: Could not extract information from the link."
        elif "Video unavailable" in simple_error:
              error_message = f"❌ Download failed: Video is unavailable."
        else:
             error_message = f"❌ Download failed. Please check the link." # Generic download error for user

        try:
             await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text=error_message)
        except Exception as edit_err:
             logger.error(f"Could not edit status message for download error: {edit_err}")

    except FileNotFoundError as e:
         logger.error(f"File not found after download attempt: {e}", exc_info=True)
         error_message = f"❌ Error: Could not find the downloaded file after processing. Please try again."
         try:
             await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text=error_message)
         except Exception as edit_err:
              logger.error(f"Could not edit status message for file not found error: {edit_err}")

    except Exception as e:
        logger.exception("An unexpected error occurred during download/upload") # Log full traceback
        error_message = f"❌ An unexpected server error occurred. Please try again later."
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text=error_message)
        except Exception as edit_err:
             logger.error(f"Could not edit status message for unexpected error: {edit_err}")
             # Send a new message if editing fails and message_id exists
             if message_id:
                 await context.bot.send_message(chat_id=chat_id, text=error_message, reply_to_message_id=message_id)

    finally:
        # Ensure processing/uploading flags are cleared and tasks cancelled
        context.user_data['processing'] = False
        context.user_data['uploading'] = False
        if 'processing_task' in locals() and not processing_task.done():
            processing_task.cancel()
        if 'upload_task' in locals() and not upload_task.done():
            upload_task.cancel()
        # Clean up the downloaded file if path was determined
        cleanup_file(downloaded_file_path)


# --- Telegram Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message when the /start command is issued."""
    user_name = update.effective_user.first_name
    await update.message.reply_text(
        f"👋 Welcome {user_name}!\nSend me a link from YouTube, SoundCloud, or TikTok, and I'll try my best to download the media for you."
        f"\nUse /help for more info."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends help message."""
    await update.message.reply_text(
        "ℹ️ **How to use this bot:**\n\n"
        "1. Send a direct link (URL) to a video or audio on:\n"
        "   - YouTube (e.g., `https://www.youtube.com/watch?v=...`)\n"
        "   - SoundCloud (e.g., `https://soundcloud.com/user/track`)\n"
        "   - TikTok (e.g., `https://www.tiktok.com/@user/video/...`)\n\n"
        "2. **For YouTube links:** I will ask if you want `MP3` (audio) or `MP4` (video).\n"
        "   - If you choose `MP4`, I'll ask for the desired quality (e.g., 720p).\n"
        "3. **For SoundCloud links:** I will automatically download the best quality MP3 audio.\n"
        "4. **For TikTok links:** I will attempt to download the video (success isn't guaranteed due to TikTok changes, might have watermark).\n\n"
        "⚠️ *Note:* Downloading very long videos or high-quality files might take some time. Large files might fail due to Telegram limits.*",
        parse_mode='Markdown' # Use Markdown for formatting
    )

# --- UPDATED handle_message Function ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming text messages containing URLs."""
    message_text = update.message.text
    if not message_text:
        return

    # Simple URL detection (can be improved)
    # Use a more robust regex to avoid matching parts of text
    urls = re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', message_text)
    if not urls:
        # Ignore messages without URLs
        return

    url = urls[0] # Process the first URL found
    url_type = get_url_type(url)
    logger.info(f"Detected URL type: {url_type} for URL: {url} from user: {update.effective_user.id}")

    if url_type == "soundcloud":
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        # Download SoundCloud as MP3
        options = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192', # Standard MP3 quality
            }],
            'quiet': True,
            'noplaylist': True, # Process only single track if URL is part of playlist
            'logger': logger, # Optional: Pass logger to yt-dlp
        }
        await download_media(url, options, update, context)

    elif url_type == "tiktok":
         await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
         # Download TikTok video (best available, might have watermark)
         options = {
             'format': 'bestvideo+bestaudio/best', # Try to get best quality video and audio
             'quiet': True,
             'noplaylist': True,
             'logger': logger,
         }
         await download_media(url, options, update, context)

    elif url_type == "youtube":
        # === START UPDATED YOUTUBE SECTION ===
        # Store the URL in user_data, associated with this chat and user
        context.user_data['pending_youtube_url'] = url
        logger.info(f"Stored youtube URL for user {update.effective_user.id}: {url}")

        # Update callback_data to NOT include the URL
        keyboard = [
            [
                InlineKeyboardButton("🎵 MP3 (Audio)", callback_data="yt_format|mp3"), # No URL here
                InlineKeyboardButton("🎬 MP4 (Video)", callback_data="yt_format|mp4"), # No URL here
            ]
        ]
        # === END UPDATED YOUTUBE SECTION ===
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text('Choose download format for YouTube link:', reply_markup=reply_markup)

    elif url_type == "unknown":
        await update.message.reply_text("⚠️ Sorry, I can only process links from YouTube, SoundCloud, and TikTok at the moment.")
    else:
         await update.message.reply_text("😕 Couldn't determine the source of the link.")

# --- UPDATED button_handler Function ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Parses the CallbackQuery and updates the message text."""
    query = update.callback_query
    await query.answer() # Answer callback query to remove "loading" state on button

    # Data format now: "action|value" e.g., "yt_format|mp3" or "yt_quality|720"
    callback_data = query.data
    parts = callback_data.split("|", 1) # Split only once now
    action = parts[0]
    value = parts[1] if len(parts) > 1 else None

    logger.info(f"Button pressed: action={action}, value={value} by user {query.from_user.id}")

    # Retrieve the stored URL associated with this user
    url_key = 'pending_youtube_url'
    url = context.user_data.get(url_key)

    if not url:
        logger.warning(f"No {url_key} found in user_data for user {query.from_user.id}")
        try:
            await query.edit_message_text("❌ Error: Could not find the original YouTube URL. Session might have expired. Please send the link again.")
        except Exception as e:
            logger.error(f"Error editing message for missing URL: {e}")
        return

    logger.info(f"Retrieved youtube URL for user {query.from_user.id}: {url}")

    # Reconstruct pseudo_update for replying to the original user message that contained the link
    # Need to handle cases where query.message.reply_to_message is None
    original_message_id = None
    if query.message.reply_to_message:
         original_message_id = query.message.reply_to_message.message_id
    else:
         # If the button message itself isn't a reply, maybe the context holds info?
         # This part is less reliable, relying on reply_to_message is safer
         logger.warning("Button message is not a reply, cannot reliably determine original message ID for reply.")
         # Fallback to replying to the button message itself, or don't reply to anything
         # original_message_id = query.message.message_id # Option: Reply to button message

    pseudo_update = type('obj', (object,), {
        'effective_chat': query.message.chat,
        # Create a mock message object only if original_message_id is known
        'effective_message': type('obj', (object,), {'message_id': original_message_id}) if original_message_id else None
    })()


    if action == "yt_format":
        if value == "mp3":
            try:
                await query.edit_message_text(text="⬇️ Preparing MP3 download...")
            except Exception as e: logger.warning(f"Could not edit message before MP3 download: {e}")

            options = {
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'quiet': True,
                'noplaylist': True,
                'logger': logger,
            }
            try:
                await download_media(url, options, pseudo_update, context)
            finally:
                # Clear the stored URL after processing
                context.user_data.pop(url_key, None)
                logger.info(f"Cleared {url_key} for user {query.from_user.id}")
                # Try deleting the button message (which is query.message)
                try:
                     await query.delete_message()
                except Exception as e:
                     logger.warning(f"Could not delete button message after MP3 processing: {e}")


        elif value == "mp4":
            # Update callback_data for quality buttons - NO URL needed here either
            keyboard = [
                [
                    InlineKeyboardButton("360p", callback_data="yt_quality|360"), # No URL
                    InlineKeyboardButton("480p", callback_data="yt_quality|480"), # No URL
                 ],
                 [
                    InlineKeyboardButton("720p", callback_data="yt_quality|720"), # No URL
                    InlineKeyboardButton("1080p", callback_data="yt_quality|1080"), # No URL
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            # Don't clear the URL yet, needed for quality selection
            try:
                await query.edit_message_text(text='Choose video quality (best available up to selection):', reply_markup=reply_markup)
            except Exception as e:
                 logger.error(f"Error editing message to ask for quality: {e}")
                 # Clear URL if we can't proceed
                 context.user_data.pop(url_key, None)
                 logger.info(f"Cleared {url_key} due to error showing quality options for user {query.from_user.id}")


    elif action == "yt_quality":
        quality = value
        try:
            await query.edit_message_text(text=f"⬇️ Preparing {quality}p MP4 download...")
        except Exception as e: logger.warning(f"Could not edit message before MP4 quality download: {e}")

        # Download YouTube video at specified quality (or best available if not found)
        options = {
            'format': f'bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best',
            'quiet': True,
            'noplaylist': True,
            'logger': logger,
            'merge_output_format': 'mp4', # Ensure output is mp4 if merging video/audio
        }
        try:
            await download_media(url, options, pseudo_update, context)
        finally:
            # Clear the stored URL after processing
            context.user_data.pop(url_key, None)
            logger.info(f"Cleared {url_key} for user {query.from_user.id}")
            # Try deleting the button message
            try:
                 await query.delete_message()
            except Exception as e:
                 logger.warning(f"Could not delete button message after MP4 quality processing: {e}")


# --- Main Function ---
def main():
    """Start the bot."""
    if not TELEGRAM_TOKEN:
        logger.error("Telegram Bot Token not found! Set TELEGRAM_BOT_TOKEN in your environment or .env file.")
        return

    # Create the Application and pass it your bot's token. Increase timeouts globally.
    application = Application.builder().token(TELEGRAM_TOKEN).read_timeout(30).write_timeout(60).connect_timeout(30).pool_timeout(30).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))

    # Message handler for URLs (ensure it doesn't conflict with commands)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Entity("url") | filters.Entity("text_link"), handle_message))
    # Fallback for messages that might contain URLs but aren't auto-detected as entities
    # This regex filter is basic, might catch unintended text. Consider refining.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r'http[s]?://'), handle_message))


    # Callback query handler for buttons
    application.add_handler(CallbackQueryHandler(button_handler))

    # Run the bot until the user presses Ctrl-C
    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES) # Process all update types
    logger.info("Bot stopped.")

if __name__ == "__main__":
    main()