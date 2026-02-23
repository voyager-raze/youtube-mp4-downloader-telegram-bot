# YouTube Video Downloader Telegram Bot

## Requirements

- Python 3.11+
- `ffmpeg` installed on your system (`sudo apt install ffmpeg` or `brew install ffmpeg`)

## Setup

1. **Install dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

2. **Create a `.env` file:**

   ```
   BOT_TOKEN=your_telegram_bot_token_here
   ```

   Get a token from [@BotFather](https://t.me/BotFather) on Telegram.

3. **Run the bot:**
   ```bash
   python yt_video_bot.py
   ```

## Features

- **Video download** in 4 quality options: 360p, 480p, 720p HD, 1080p FHD
- **Subtitle download** — automatically fetches English subtitles (.srt) and sends as a separate file
- **Playlist support** — send a playlist URL and choose how many videos to download
- **Multiple links** — send several links at once in one message
- **Per-user queue** — each user has their own download queue processed one at a time
- **Cancel support** — `/cancel` stops all pending downloads
- **Status check** — `/status` shows your current queue

## Commands

| Command   | Description                  |
| --------- | ---------------------------- |
| `/start`  | Show welcome message         |
| `/status` | Check your download queue    |
| `/cancel` | Cancel all pending downloads |

## Notes

- **Telegram file size limit**: 50MB per file. Large videos at high quality may exceed this. Use 360p or 480p for long videos.
- **Subtitles**: Auto-generated subtitles are used if manual ones aren't available. If no subtitles exist, the bot will notify you.
- **Downloads folder**: Temporary files are saved to `./downloads/` and deleted after sending.
