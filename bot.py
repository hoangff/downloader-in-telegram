# -*- coding: utf-8 -*-
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
    if re.search(r"(youtube\.com|youtu\.be)", url, re.IGNORECASE):
        return "youtube"
    elif re.search(r"soundcloud\.com", url, re.IGNORECASE):
        return "soundcloud"
    elif re.search(r"tiktok\.com", url, re.IGNORECASE):
        return "tiktok"
    else:
        return "unknown"

async def send_typing_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends typing action periodically."""
    if not update or not hasattr(update, 'effective_chat') or not update.effective_chat: return
    chat_id = update.effective_chat.id
    processing_key = f'processing_{chat_id}' # Use chat-specific key
    while context.user_data.get(processing_key, False):
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception as e:
            logger.warning(f"Failed to send typing action to {chat_id}: {e}")
            break
        await asyncio.sleep(4)

async def send_upload_action(update: Update, context: ContextTypes.DEFAULT_TYPE, file_type: str):
    """Sends uploading action periodically."""
    if not update or not hasattr(update, 'effective_chat') or not update.effective_chat: return
    chat_id = update.effective_chat.id
    uploading_key = f'uploading_{chat_id}' # Use chat-specific key
    action = ChatAction.UPLOAD_VIDEO if file_type == 'video' else ChatAction.UPLOAD_AUDIO
    while context.user_data.get(uploading_key, False):
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=action)
        except Exception as e:
            logger.warning(f"Failed to send upload action to {chat_id}: {e}")
            break
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
async def run_youtubedl(options: dict):
    """Runs yt-dlp synchronously in a separate thread."""
    loop = asyncio.get_running_loop()
    def download():
        options['progress_hooks'] = []
        options['quiet'] = True
        with yt_dlp.YoutubeDL(options) as ydl:
            try:
                if 'urls' not in options or not options['urls']: raise ValueError("URL missing")
                logger.debug(f"Starting yt-dlp download with options: {options}")
                info_dict = ydl.extract_info(options['urls'][0], download=True)
                return info_dict
            except yt_dlp.utils.DownloadError as e:
                logger.error(f"yt-dlp DownloadError: {e}")
                raise
            except Exception as e:
                logger.error(f"yt-dlp generic error: {e}", exc_info=True)
                raise
    info_dict = await loop.run_in_executor(None, download)
    return info_dict

# --- download_media Function (handles download, upload, delete) ---
async def download_media(url: str, format_options: dict, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Downloads media, uploads to Telegram, and deletes original msg on success."""
    chat_id = None
    message_id = None # Original user message ID to potentially DELETE
    status_message = None # Bot's status message

    if hasattr(update, 'effective_chat') and update.effective_chat: chat_id = update.effective_chat.id
    if hasattr(update, 'effective_message') and update.effective_message: message_id = update.effective_message.message_id
    if not chat_id: logger.error("download_media called without valid chat_id."); return

    status_text = "⏳ Processing link..."
    try:
        reply_id = message_id if message_id else None # Reply if we have the original message ID
        status_message = await context.bot.send_message(chat_id=chat_id, text=status_text, reply_to_message_id=reply_id)
    except Exception as e:
        logger.error(f"Failed to send initial status message to {chat_id}: {e}"); return

    processing_key = f'processing_{chat_id}'
    uploading_key = f'uploading_{chat_id}'
    context.user_data[processing_key] = True
    processing_task = asyncio.create_task(send_typing_action(update, context))

    format_options['urls'] = [url]
    output_filename = os.path.join(DOWNLOAD_PATH, f"{chat_id}_{uuid4()}.%(ext)s")
    format_options['outtmpl'] = output_filename
    downloaded_file_path = None
    info_dict = None

    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text="⏳ Downloading...")
        info_dict = await run_youtubedl(format_options)
        context.user_data[processing_key] = False
        if not processing_task.done(): processing_task.cancel()

        # --- Robust file path finding ---
        actual_filepath = None
        if info_dict and 'requested_downloads' in info_dict and info_dict['requested_downloads']:
            actual_filepath = info_dict['requested_downloads'][0].get('filepath')
        if not actual_filepath and info_dict: actual_filepath = info_dict.get('filepath') or info_dict.get('_filename')
        if not actual_filepath and info_dict and format_options.get('outtmpl'):
             guessed_path = format_options['outtmpl']
             if '%(ext)s' in guessed_path and info_dict.get('ext'): guessed_path = guessed_path.replace('%(ext)s', info_dict['ext'])
             if '%(ext)s' in guessed_path or guessed_path == output_filename:
                  base_name_part = f"{chat_id}_{os.path.basename(output_filename).split('.')[0]}"
                  found = False
                  for f in os.listdir(DOWNLOAD_PATH):
                       if os.path.basename(f).startswith(base_name_part): actual_filepath = os.path.join(DOWNLOAD_PATH, f); found = True; break
             elif os.path.exists(guessed_path): actual_filepath = guessed_path
        if not actual_filepath or not os.path.exists(actual_filepath):
            logger.error(f"Could not determine downloaded file path. Template: {output_filename}.")
            raise FileNotFoundError(f"Could not locate downloaded file: {output_filename}.")
        downloaded_file_path = actual_filepath
        logger.info(f"Download complete. Path: {downloaded_file_path}")
        # --- End file path finding ---

        file_type = 'video' if format_options.get('format', '').startswith(('bv', 'bestvideo', 'mp4')) else 'audio'
        await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text=f"⬆️ Uploading {file_type}...")
        context.user_data[uploading_key] = True
        upload_task = asyncio.create_task(send_upload_action(update, context, file_type))

        title = info_dict.get('title', 'Downloaded Media')
        duration = info_dict.get('duration')

        # --- Sending Logic ---
        file_sent_successfully = False
        try:
            send_kwargs = {'chat_id': chat_id, 'title': title, 'duration': duration, 'reply_to_message_id': reply_id, 'write_timeout': 180}
            with open(downloaded_file_path, 'rb') as f:
                if file_type == 'audio': await context.bot.send_audio(audio=f, **send_kwargs)
                else:
                    width = info_dict.get('width'); height = info_dict.get('height')
                    send_kwargs.update({'caption': title, 'width': width, 'height': height, 'supports_streaming': True}); send_kwargs.pop('title', None)
                    await context.bot.send_video(video=f, **send_kwargs)
            file_sent_successfully = True
            await status_message.delete()

            # <<< --- DELETE ORIGINAL USER MESSAGE ON SUCCESS --- >>>
            if message_id:
                try:
                    await asyncio.sleep(0.5) # Small delay
                    await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
                    logger.info(f"Successfully deleted original message {message_id} in chat {chat_id}")
                except Exception as delete_error:
                    logger.warning(f"Could not delete original message {message_id} in {chat_id}: {delete_error}")
            else: logger.warning(f"No original message_id found to delete in {chat_id}")
            # <<< --- END DELETE ORIGINAL --- >>>

        except Exception as send_error:
             logger.error(f"Error sending file to Telegram: {send_error}", exc_info=True)
             error_detail = str(send_error)
             if "Request Entity Too Large" in error_detail: user_error = "❌ Error: File is too large for Telegram."
             elif "Timed out" in error_detail: user_error = "❌ Error: Upload timed out."
             else: user_error = "❌ Error sending file."
             try: await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text=user_error)
             except Exception: pass # Ignore edit error if status msg already deleted/inaccessible
        finally:
             context.user_data[uploading_key] = False
             if 'upload_task' in locals() and not upload_task.done(): upload_task.cancel()
        # --- End Sending Logic ---

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Download failed: {e}")
        error_parts = str(e).split(':'); simple_error = error_parts[-1].strip() if len(error_parts) > 1 else str(e)
        if "Unsupported URL" in simple_error: user_error = "❌ Download failed: Unsupported URL."
        elif "Unable to extract" in simple_error: user_error = "❌ Download failed: Invalid link."
        elif "Video unavailable" in simple_error: user_error = "❌ Download failed: Video unavailable."
        else: user_error = "❌ Download failed."
        try: await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text=user_error)
        except Exception: pass

    except FileNotFoundError as e:
         logger.error(f"File not found after download attempt: {e}", exc_info=True)
         user_error = "❌ Error: Could not find downloaded file."
         try: await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text=user_error)
         except Exception: pass

    except Exception as e:
        logger.exception(f"Unexpected error in download_media for chat {chat_id}")
        user_error = "❌ An unexpected server error occurred."
        try: await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text=user_error)
        except Exception: pass

    finally:
        context.user_data.pop(processing_key, None)
        context.user_data.pop(uploading_key, None)
        if 'processing_task' in locals() and not processing_task.done(): processing_task.cancel()
        if 'upload_task' in locals() and not upload_task.done(): upload_task.cancel()
        cleanup_file(downloaded_file_path)


# --- Telegram Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    await update.message.reply_text(f"👋 Welcome {user_name}!\nSend link (YouTube, SoundCloud, TikTok). /help for info.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ **How to use:**\n"
        "1. Send direct link (YouTube, SoundCloud, TikTok).\n"
        "2. **YouTube:** Choose MP3/MP4, then quality if MP4.\n"
        "3. **SoundCloud:** Downloads MP3 .\n" # Updated help text
        "4. **TikTok:** Downloads video.\n\n"
        "✅ Original message deleted after successful send.\n"
        "⚠️ Large files may fail due to Telegram limits.",
        parse_mode='Markdown'
    )

# --- handle_message Function ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    message_text = update.message.text
    urls = re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', message_text)
    if not urls: return

    url = urls[0]; url_type = get_url_type(url)
    logger.info(f"URL: {url_type} from user: {update.effective_user.id}")

    if url_type == "soundcloud":
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        options = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                # <<< THAY ĐỔI BITRATE >>>
                'preferredquality': '128', # Changed from '192'
            }],
            'quiet': True, 'noplaylist': True, 'logger': logger,
        }
        await download_media(url, options, update, context)

    elif url_type == "tiktok":
         await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
         options = {'format': 'bestvideo+bestaudio/best','quiet': True, 'noplaylist': True, 'logger': logger,}
         await download_media(url, options, update, context)

    elif url_type == "youtube":
        url_key = f'yt_url_{update.effective_chat.id}_{update.effective_user.id}' # Unique key per user/chat
        context.user_data[url_key] = url
        logger.info(f"Stored key {url_key} for user {update.effective_user.id}")
        keyboard = [[
            InlineKeyboardButton("🎵 MP3 (~128kbps)", callback_data=f"yt_format|mp3|{url_key}"), # Updated label slightly
            InlineKeyboardButton("🎬 MP4 (Video)", callback_data=f"yt_format|mp4|{url_key}"),
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
             # Ensure replying to the original message
             await update.message.reply_text('Choose download format:', reply_markup=reply_markup, reply_to_message_id=update.message.message_id)
        except Exception as e:
             logger.error(f"Failed to send YouTube options: {e}")
             # Clean up context if sending options failed
             context.user_data.pop(url_key, None)

    elif url_type == "unknown":
        await update.message.reply_text("⚠️ Unsupported link source.")


# --- button_handler Function ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    callback_data = query.data
    parts = callback_data.split("|", 2)
    if len(parts) < 3: logger.error(f"Invalid callback_data: {callback_data}"); return

    action, value, url_key = parts[0], parts[1], parts[2]
    logger.info(f"Button: action={action}, value={value}, key={url_key} by user {query.from_user.id}")

    url = context.user_data.get(url_key)
    if not url:
        logger.warning(f"No URL for key {url_key}"); await query.edit_message_text("❌ Error: Request expired. Send link again."); return

    original_message_id = query.message.reply_to_message.message_id if query.message.reply_to_message else None
    pseudo_update = type('obj', (object,), {'effective_chat': query.message.chat,'effective_message': type('obj', (object,), {'message_id': original_message_id}) if original_message_id else None})()

    if action == "yt_format":
        if value == "mp3":
            try: await query.edit_message_text(text="⬇️ Preparing MP3 (~128kbps)...")
            except Exception: pass
            options = {
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    # <<< THAY ĐỔI BITRATE >>>
                    'preferredquality': '128', # Changed from '192'
                }],
                'quiet': True, 'noplaylist': True, 'logger': logger,
            }
            try: await download_media(url, options, pseudo_update, context)
            finally: context.user_data.pop(url_key, None); logger.info(f"Cleared {url_key}")

        elif value == "mp4":
            keyboard = [[ InlineKeyboardButton(q, callback_data=f"yt_quality|{q.replace('p','')}|{url_key}") for q in ["360p", "480p"] ],
                        [ InlineKeyboardButton(q, callback_data=f"yt_quality|{q.replace('p','')}|{url_key}") for q in ["720p", "1080p"] ]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            try: await query.edit_message_text(text='Choose video quality:', reply_markup=reply_markup)
            except Exception as e: logger.error(f"Error asking quality: {e}"); context.user_data.pop(url_key, None)

    elif action == "yt_quality":
        quality = value
        try: await query.edit_message_text(text=f"⬇️ Preparing {quality}p MP4...")
        except Exception: pass
        options = {
            'format': f'bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best',
            'quiet': True, 'noplaylist': True, 'logger': logger, 'merge_output_format': 'mp4',
        }
        try: await download_media(url, options, pseudo_update, context)
        finally: context.user_data.pop(url_key, None); logger.info(f"Cleared {url_key}")


# --- Main Function ---
def main():
    if not TELEGRAM_TOKEN: logger.error("TELEGRAM_BOT_TOKEN not found!"); return
    app = Application.builder().token(TELEGRAM_TOKEN).read_timeout(30).write_timeout(60).connect_timeout(30).pool_timeout(30).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    url_filters = filters.Entity("url") | filters.Entity("text_link")
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & url_filters, handle_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r'http[s]?://'), handle_message)) # Fallback regex
    app.add_handler(CallbackQueryHandler(button_handler))
    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Bot stopped.")

if __name__ == "__main__":
    main()
