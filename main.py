import os
import signal
import logging
import asyncio
import aiohttp
from aiohttp import web
from telegram.ext import Application, ContextTypes
from telegram.constants import ParseMode

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", "8080"))

CHANNEL_USERNAME = "@zanimeart"
POST_INTERVAL = 60  # 1 hour in seconds
SEARCH_TAG = "wonbin_lee"  # Target testing artist

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Single Aiohttp Session headers
UA = {"User-Agent": "ZAnimeArtBot/1.0"}

# ─────────────────────────────────────────────
# Tiny Web Site for Render (Health Check)
# ─────────────────────────────────────────────
HTML_PAGE = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>ZAnimeArt Bot</title>
    <style>
        body {{ background:#0f172a; color:#e2e8f0; font-family:system-ui,sans-serif; display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
        .card {{ background:#1e293b; padding:48px; border-radius:16px; text-align:center; box-shadow:0 20px 40px rgba(0,0,0,.5); max-width:420px; border-top: 4px solid #38bdf8; }}
        h1 {{ color:#38bdf8; margin:0 0 8px; }}
        .status {{ color:#4ade80; font-weight:700; font-size:1.1em; margin:16px 0; display: inline-flex; align-items: center; gap: 6px; }}
        .dot {{ width: 10px; height: 10px; background-color: #4ade80; border-radius: 50%; box-shadow: 0 0 8px #4ade80; }}
        p {{ color:#94a3b8; line-height:1.6; margin-bottom: 24px; }}
        .tag {{ display:inline-block; background:#334155; color:#cbd5e1; padding:6px 12px; border-radius:20px; font-size: 0.9em; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>🎨 ZAnimeArt Bot</h1>
        <div class="status"><div class="dot"></div> Online & Running</div>
        <p>This bot automatically fetches high-quality anime art and broadcasts it to the Telegram channel.</p>
        <div class="tag">Target: #{SEARCH_TAG}</div>
        <div class="tag" style="margin-top: 8px;">Channel: {CHANNEL_USERNAME}</div>
    </div>
</body>
</html>"""

async def web_index(request):
    """Returns the simple HTML page so Render knows the app is alive."""
    return web.Response(text=HTML_PAGE, content_type="text/html")


# ─────────────────────────────────────────────
# Danbooru Fetcher & Formatter
# ─────────────────────────────────────────────
async def fetch_random_danbooru_post(search_tag: str) -> dict:
    """Fetches a random post from Danbooru using the provided tag."""
    # Danbooru limits anonymous searches to 2 tags. 
    # Tag 1: wonbin_lee | Tag 2: rating:general (SFW)
    url = f"https://danbooru.donmai.us/posts/random.json?tags={search_tag}+rating:general"
    
    async with aiohttp.ClientSession(headers=UA) as session:
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, dict) and "file_url" in data:
                        return data
                else:
                    logger.error(f"Danbooru API Error: {response.status}")
        except Exception as e:
            logger.error(f"Failed to fetch image: {e}")
    return {}

def format_post_data(post: dict) -> tuple:
    """Extracts and formats details like artist, character, links, and hashtags."""
    file_url = post.get("file_url")
    large_file_url = post.get("large_file_url", file_url)

    # 1. Extract Details
    artist_raw = post.get("tag_string_artist", "Unknown").split()[0]
    character_raw = post.get("tag_string_character", "Original").split()[0]
    
    artist_name = artist_raw.replace("_", " ").title()
    character_name = character_raw.replace("_", " ").title()

    # 2. Artist Source Link
    source_url = post.get("source")
    if not source_url or not source_url.startswith("http"):
        post_id = post.get("id")
        source_url = f"https://danbooru.donmai.us/posts/{post_id}"

    # 3. Hashtags (Max 3)
    ht_artist = artist_raw.replace("_", "").replace("-", "")
    ht_character = character_raw.replace("_", "").replace("-", "")
    
    tags =[]
    if ht_artist and ht_artist != "unknown": tags.append(f"#{ht_artist}")
    if ht_character and ht_character != "original": tags.append(f"#{ht_character}")
    tags.append("#AnimeArt")
    
    hashtags_str = " ".join(tags[:3]) # Limit strictly to 3 tags

    # 4. Styled Caption
    caption = (
        f"🎨 <b>Artist:</b> <a href='{source_url}'>{artist_name}</a>\n"
        f"👤 <b>Character:</b> {character_name}\n\n"
        f"{hashtags_str}\n\n"
        f"✨ <b>Join us:</b> {CHANNEL_USERNAME}"
    )
    
    return file_url, large_file_url, caption


# ─────────────────────────────────────────────
# Scheduled Job
# ─────────────────────────────────────────────
async def auto_post_job(context: ContextTypes.DEFAULT_TYPE):
    """Fetches and posts the image to the channel."""
    logger.info(f"Fetching new art for {SEARCH_TAG}...")
    
    post = await fetch_random_danbooru_post(SEARCH_TAG)
    if not post:
        logger.warning("No valid post found. Skipping this cycle.")
        return

    file_url, large_file_url, caption = format_post_data(post)

    try:
        # Message 1: The Broadcast Photo (optimized for viewing)
        await context.bot.send_photo(
            chat_id=CHANNEL_USERNAME,
            photo=large_file_url,
            caption=caption,
            parse_mode=ParseMode.HTML
        )
        
        # Message 2: The Full Quality File Document (uncompressed)
        await context.bot.send_document(
            chat_id=CHANNEL_USERNAME,
            document=file_url,
            caption="📁 <b>Full Quality Source</b>",
            parse_mode=ParseMode.HTML
        )
        
        logger.info("Successfully posted image and document to channel!")
        
    except Exception as e:
        logger.error(f"Error posting to Telegram: {e}")


# ─────────────────────────────────────────────
# Main Application Runtime
# ─────────────────────────────────────────────
async def main():
    if not BOT_TOKEN:
        logger.error("TELEGRAM_TOKEN is missing! Set it in your environment variables.")
        return

    # 1. Setup Telegram Bot Application
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Schedule the auto-post job
    job_queue = app.job_queue
    logger.info(f"Scheduling auto-post every {POST_INTERVAL} seconds...")
    # 'first=10' means it will make its first post 10 seconds after starting
    job_queue.run_repeating(auto_post_job, interval=POST_INTERVAL, first=10)

    # 2. Setup Aiohttp Web Server
    web_app = web.Application()
    web_app.router.add_get("/", web_index)
    
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    
    # Start both the Web Server and the Bot
    await site.start()
    logger.info(f"🚀 Web Server started on port {PORT}")
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("🤖 Bot is now polling and active.")

    # 3. Keep the loop running until stopped manually or by Render
    stop_signal = asyncio.Event()
    loop = asyncio.get_running_loop()
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_signal.set)

    await stop_signal.wait()

    # 4. Graceful Shutdown
    logger.info("Shutting down...")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    await site.stop()
    await runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
