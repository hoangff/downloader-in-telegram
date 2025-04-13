import os
import yt_dlp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# Load biến môi trường từ .env
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")

# Hàm tải và gửi media
async def download_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    chat_id = update.effective_chat.id

    if not url.startswith(("http://", "https://")):
        await context.bot.send_message(chat_id=chat_id, text="❗ Vui lòng gửi một đường dẫn hợp lệ.")
        return

    await context.bot.send_message(chat_id=chat_id, text="🔄 Đang tải xuống, vui lòng chờ...")

    try:
        # Cấu hình yt-dlp
        ydl_opts = {
            'outtmpl': 'downloads/%(title)s.%(ext)s',
            'format': 'bestvideo+bestaudio/best',
            'merge_output_format': 'mp4',
            'quiet': True,
            'noplaylist': True,
            'socket_timeout': 180,
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
        }

        # Nếu là link nhạc (SoundCloud, Music YouTube, ...), chỉ tải audio
        if any(x in url for x in ["soundcloud.com", "music.youtube.com"]):
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]

        # Tải về bằng yt-dlp
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = None

            if 'requested_downloads' in info:
                for f in info['requested_downloads']:
                    if 'filepath' in f:
                        file_path = f['filepath']
                        break

            if not file_path:
                file_path = ydl.prepare_filename(info)

            if file_path.endswith(('.webm', '.m4a', '.opus')):
                file_path = os.path.splitext(file_path)[0] + ".mp3"

        # Gửi file dựa trên kích thước
        file_size = os.path.getsize(file_path)
        with open(file_path, 'rb') as f:
            if file_path.endswith('.mp3'):
                if file_size > 50 * 1024 * 1024:
                    await context.bot.send_document(chat_id=chat_id, document=f, filename=os.path.basename(file_path))
                else:
                    await context.bot.send_audio(chat_id=chat_id, audio=f, title=info.get("title", "Tệp âm thanh"))
            else:
                if file_size > 50 * 1024 * 1024:
                    await context.bot.send_document(chat_id=chat_id, document=f, filename=os.path.basename(file_path))
                else:
                    await context.bot.send_video(chat_id=chat_id, video=f, supports_streaming=True)

        os.remove(file_path)

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Lỗi khi tải: {e}")

# Lệnh /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="👋 Gửi link YouTube, TikTok hoặc SoundCloud để tải video hoặc nhạc!"
    )

# Main
def main():
    os.makedirs("downloads", exist_ok=True)
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), download_media))

    print("✅ Bot đã khởi chạy!")
    app.run_polling()

if __name__ == "__main__":
    main()
