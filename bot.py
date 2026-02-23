import os
import asyncio
import logging
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

# ------------------ LOGGING ------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------ CONFIG ------------------

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN not set")

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Telegram file size limit (50MB for bots without large file support)
MAX_FILE_SIZE_MB = 50
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# Conversation states
WAITING_FOR_QUALITY = 1
WAITING_FOR_COUNT = 2

# Per-user state
user_queues: dict[int, asyncio.Queue] = {}
user_cancel_flags: dict[int, bool] = defaultdict(bool)
user_queue_tasks: dict[int, asyncio.Task] = {}

# Quality format map: label -> (format_selector, height_label)
VIDEO_QUALITIES = {
    "360": ("bestvideo[height<=360]+bestaudio/best[height<=360]", "360p"),
    "480": ("bestvideo[height<=480]+bestaudio/best[height<=480]", "480p"),
    "720": ("bestvideo[height<=720]+bestaudio/best[height<=720]", "720p"),
    "1080": ("bestvideo[height<=1080]+bestaudio/best[height<=1080]", "1080p"),
}

# ------------------ YTDLP OPTIONS ------------------

def ydl_base_opts(quality: str = "720") -> dict:
    fmt = VIDEO_QUALITIES.get(quality, VIDEO_QUALITIES["720"])[0]
    return {
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android"],
                "skip": ["dash", "hls"],
            }
        },
        "format": fmt,
        "outtmpl": f"{DOWNLOAD_DIR}/%(title)s.%(ext)s",
        "merge_output_format": "mp4",
        "postprocessors": [
            {
                "key": "FFmpegMetadata",
                "add_metadata": True,
            },
        ],
        # Download subtitles
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en", "en-US"],
        "subtitlesformat": "srt",
        "sleep_interval": 2,
        "max_sleep_interval": 5,
        "sleep_interval_requests": 2,
    }

# ------------------ HELPERS ------------------

def extract_links(text: str) -> list[str]:
    parts = text.replace("\n", " ").split()
    return [p for p in parts if "youtube.com" in p or "youtu.be" in p]

def get_user_queue(user_id: int) -> asyncio.Queue:
    if user_id not in user_queues:
        user_queues[user_id] = asyncio.Queue()
    return user_queues[user_id]

def cleanup_files(base_path: str):
    """Delete video and any leftover subtitle/temp files."""
    for ext in [".mp4", ".mkv", ".webm", ".srt", ".vtt", ".ass", ".jpg", ".jpeg", ".png", ".webp"]:
        try:
            candidate = base_path + ext
            if os.path.exists(candidate):
                os.remove(candidate)
        except Exception:
            pass
    # Also try to remove any .en.srt, .en-US.srt, etc.
    try:
        folder = os.path.dirname(base_path)
        stem = os.path.basename(base_path)
        for f in os.listdir(folder):
            if f.startswith(stem) and f != stem:
                full = os.path.join(folder, f)
                try:
                    os.remove(full)
                except Exception:
                    pass
    except Exception:
        pass

def find_subtitle_file(base_path: str) -> str | None:
    """Find any subtitle file matching the base path."""
    folder = os.path.dirname(base_path) or "."
    stem = os.path.basename(base_path)
    for f in os.listdir(folder):
        if f.startswith(stem) and any(f.endswith(ext) for ext in [".srt", ".vtt", ".ass"]):
            return os.path.join(folder, f)
    return None

def get_file_size_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)

# ------------------ CORE DOWNLOAD ------------------

async def download_and_send(bot, chat_id: int, url: str, quality: str = "720") -> bool:
    """Download one video and send it with subtitle. Returns True on success."""
    loop = asyncio.get_running_loop()
    video_path = None
    base_path = None

    def run():
        opts = ydl_base_opts(quality)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            # Ensure .mp4 extension
            base = filename.rsplit(".", 1)[0]
            mp4 = base + ".mp4"
            return mp4, base, info.get("title", "Unknown"), info.get("uploader", "Unknown"), info.get("duration", 0)

    try:
        video_path, base_path, title, author, duration = await loop.run_in_executor(None, run)

        if not os.path.exists(video_path):
            await bot.send_message(chat_id=chat_id, text=f"⚠️ File missing after download: {title}")
            return False

        file_size = get_file_size_mb(video_path)
        quality_label = VIDEO_QUALITIES.get(quality, ("", quality + "p"))[1]

        if file_size > MAX_FILE_SIZE_MB:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ *{title}* ({quality_label}) is {file_size:.1f}MB — exceeds Telegram's {MAX_FILE_SIZE_MB}MB limit.\n"
                    f"Try a lower quality (360p or 480p)."
                ),
                parse_mode="Markdown",
            )
            return False

        caption = f"🎬 *{title}*\n👤 {author}\n📐 {quality_label} | 💾 {file_size:.1f}MB"

        # Send video
        with open(video_path, "rb") as video_file:
            await bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption=caption,
                parse_mode="Markdown",
                supports_streaming=True,
                duration=duration if duration else None,
            )

        # Send subtitle if available
        subtitle_path = find_subtitle_file(base_path)
        if subtitle_path:
            sub_ext = os.path.splitext(subtitle_path)[1]
            safe_title = title[:40].replace("/", "-").replace("\\", "-")
            sub_filename = f"{safe_title}{sub_ext}"
            with open(subtitle_path, "rb") as sub_file:
                await bot.send_document(
                    chat_id=chat_id,
                    document=sub_file,
                    filename=sub_filename,
                    caption=f"📝 Subtitles for: {title}",
                )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=f"ℹ️ No subtitles available for: {title}",
            )

        return True

    except Exception as e:
        logger.error(f"Download/send failed for {url}: {e}")
        await bot.send_message(chat_id=chat_id, text=f"❌ Download failed: {str(e)[:100]}")
        return False

    finally:
        if base_path:
            cleanup_files(base_path)

# ------------------ QUEUE WORKER ------------------

async def queue_worker(user_id: int, bot, chat_id: int):
    """Process one download at a time per user."""
    q = get_user_queue(user_id)

    while True:
        try:
            item = await asyncio.wait_for(q.get(), timeout=300)
        except asyncio.TimeoutError:
            user_queue_tasks.pop(user_id, None)
            return

        if item is None:
            q.task_done()
            user_queue_tasks.pop(user_id, None)
            return

        url, quality, total, current = item

        if user_cancel_flags.get(user_id):
            q.task_done()
            while not q.empty():
                try:
                    q.get_nowait()
                    q.task_done()
                except asyncio.QueueEmpty:
                    break
            user_cancel_flags[user_id] = False
            user_queue_tasks.pop(user_id, None)
            await bot.send_message(chat_id=chat_id, text="🚫 All downloads cancelled.")
            return

        await bot.send_message(chat_id=chat_id, text=f"📥 Downloading {current}/{total}...")
        ok = await download_and_send(bot, chat_id, url, quality)
        if not ok:
            await bot.send_message(chat_id=chat_id, text=f"⚠️ Skipped {current}/{total}")

        q.task_done()

        if q.empty():
            await bot.send_message(chat_id=chat_id, text="✅ All done!")
            user_queue_tasks.pop(user_id, None)
            return

async def enqueue_downloads(bot, chat_id: int, user_id: int, urls: list[str], quality: str):
    """Push URLs into the user's queue and start a worker if needed."""
    q = get_user_queue(user_id)
    total = len(urls)
    for i, url in enumerate(urls, start=1):
        await q.put((url, quality, total, i))

    existing = user_queue_tasks.get(user_id)
    if existing is None or existing.done():
        task = asyncio.create_task(queue_worker(user_id, bot, chat_id))
        user_queue_tasks[user_id] = task

# ------------------ HANDLERS ------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to the YouTube Video Downloader Bot!\n\n"
        "📌 *How to use:*\n"
        "• Send one or more YouTube links\n"
        "• Choose your preferred video quality\n"
        "• Receive the video + subtitle file (if available)\n\n"
        "📋 *Playlist support:* Send a playlist link and choose how many videos to download.\n\n"
        "⚠️ *File size limit:* 50MB per video. Use lower quality for large videos.\n\n"
        "🔧 *Commands:*\n"
        "/cancel — stop ongoing downloads\n"
        "/status — check your queue",
        parse_mode="Markdown",
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: Detect links and ask for quality."""
    text = update.message.text.strip()
    links = extract_links(text)

    if not links:
        await update.message.reply_text("❌ No valid YouTube links found. Please send a YouTube URL.")
        return ConversationHandler.END

    context.user_data["pending_links"] = links

    keyboard = [[
        InlineKeyboardButton("📱 360p", callback_data="quality_360"),
        InlineKeyboardButton("💻 480p", callback_data="quality_480"),
    ], [
        InlineKeyboardButton("🖥️ 720p HD", callback_data="quality_720"),
        InlineKeyboardButton("📺 1080p FHD", callback_data="quality_1080"),
    ]]
    await update.message.reply_text(
        f"🎬 Found *{len(links)}* link(s). Choose video quality:\n\n"
        "⚠️ Higher quality = larger file. Telegram limit is 50MB.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return WAITING_FOR_QUALITY

async def handle_quality_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: Quality chosen — analyze links."""
    query = update.callback_query
    await query.answer()

    quality = query.data.split("_")[1]
    quality_label = VIDEO_QUALITIES.get(quality, ("", quality + "p"))[1]
    context.user_data["quality"] = quality

    await query.edit_message_text(f"✅ Quality: *{quality_label}* — analyzing links...", parse_mode="Markdown")

    links = context.user_data.get("pending_links", [])
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    bot = context.bot

    for index, link in enumerate(links, start=1):
        try:
            loop = asyncio.get_running_loop()

            def extract_info(u=link):
                with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True}) as ydl:
                    return ydl.extract_info(u, download=False)

            info = await loop.run_in_executor(None, extract_info)

        except Exception as e:
            logger.error(f"Failed to analyze {link}: {e}")
            await bot.send_message(chat_id=chat_id, text=f"❌ Failed to analyze link {index}.")
            continue

        # ---------- PLAYLIST ----------
        if info.get("_type") == "playlist":
            entries = []
            for e in info.get("entries", []):
                if not e:
                    continue
                url = e.get("url") or e.get("id", "")
                if url and not url.startswith("http"):
                    url = f"https://www.youtube.com/watch?v={url}"
                if url:
                    entries.append(url)

            total_videos = len(entries)
            playlist_title = info.get("title", "Unknown Playlist")

            context.user_data["playlist_entries"] = entries
            context.user_data["playlist_total"] = total_videos
            context.user_data["remaining_links"] = links[index:]

            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"📂 *Playlist:* {playlist_title}\n"
                    f"🎬 Contains *{total_videos}* videos\n\n"
                    f"How many videos do you want to download?\n"
                    f"Reply with a number (1–{total_videos}) or *all*."
                ),
                parse_mode="Markdown",
            )
            return WAITING_FOR_COUNT

        # ---------- SINGLE VIDEO ----------
        else:
            title = info.get("title", link)
            duration = info.get("duration", 0)
            dur_str = f"{duration // 60}m {duration % 60}s" if duration else "unknown duration"
            await bot.send_message(
                chat_id=chat_id,
                text=f"⬇️ Queuing [{index}/{len(links)}]: *{title}* ({dur_str})",
                parse_mode="Markdown",
            )
            await enqueue_downloads(bot, chat_id, user_id, [link], quality)

    return ConversationHandler.END

async def handle_playlist_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 3 (playlist only): How many videos to download."""
    user_input = update.message.text.strip().lower()
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    bot = context.bot

    playlist_entries = context.user_data.get("playlist_entries", [])
    total_videos = context.user_data.get("playlist_total", 0)
    remaining_links = context.user_data.get("remaining_links", [])
    quality = context.user_data.get("quality", "720")

    if not playlist_entries:
        await update.message.reply_text("❌ Playlist data missing. Please resend the link.")
        return ConversationHandler.END

    if user_input == "all":
        count = total_videos
    else:
        try:
            count = int(user_input)
            if count < 1 or count > total_videos:
                await update.message.reply_text(
                    f"❌ Enter a number between 1 and {total_videos}, or *all*.",
                    parse_mode="Markdown",
                )
                return WAITING_FOR_COUNT
        except ValueError:
            await update.message.reply_text(
                f"❌ Invalid input. Enter a number between 1 and {total_videos}, or *all*.",
                parse_mode="Markdown",
            )
            return WAITING_FOR_COUNT

    quality_label = VIDEO_QUALITIES.get(quality, ("", quality + "p"))[1]
    selected = playlist_entries[:count]
    await update.message.reply_text(
        f"⬇️ Queuing *{count}* video(s) from playlist at *{quality_label}*...\n"
        f"Each video will be sent with subtitles if available.",
        parse_mode="Markdown",
    )
    await enqueue_downloads(bot, chat_id, user_id, selected, quality)

    context.user_data.pop("playlist_entries", None)
    context.user_data.pop("playlist_total", None)
    context.user_data.pop("remaining_links", None)

    if remaining_links:
        await update.message.reply_text(f"🔄 Also queuing {len(remaining_links)} additional link(s)...")
        await enqueue_downloads(bot, chat_id, user_id, remaining_links, quality)

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel all pending downloads for this user."""
    user_id = update.effective_user.id

    user_cancel_flags[user_id] = True
    context.user_data.clear()

    q = get_user_queue(user_id)
    await q.put(None)

    await update.message.reply_text(
        "🚫 Cancel requested. Current download will finish, then everything stops."
    )
    return ConversationHandler.END

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show queue size for this user."""
    user_id = update.effective_user.id
    q = user_queues.get(user_id)
    task = user_queue_tasks.get(user_id)

    is_working = task is not None and not task.done()
    queue_size = q.qsize() if q else 0

    if is_working and queue_size == 0:
        await update.message.reply_text("📥 Downloading now, nothing else waiting.")
    elif is_working:
        await update.message.reply_text(f"📥 Downloading now + *{queue_size}* more waiting in queue.", parse_mode="Markdown")
    else:
        await update.message.reply_text("✅ Your queue is empty. Nothing downloading.")

# ------------------ RUN ------------------

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)],
        states={
            WAITING_FOR_QUALITY: [
                CallbackQueryHandler(handle_quality_selection, pattern="^quality_")
            ],
            WAITING_FOR_COUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_playlist_count)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(conv_handler)

    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()