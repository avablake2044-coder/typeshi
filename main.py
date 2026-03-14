import os
import io
import re
import random
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
LOG_GROUP_ID = "-5137021203"  # Your group for storing IDs
POST_INTERVAL = 90 # 1 hour in seconds

# Memory to track duplicates
SEEN_IDS = set()

# 50 High-Quality Artists Similar to wonbin_lee
ARTISTS =[
    "wonbin_lee", "torino_aqua", "wlop", "mika_pikazo", "rurudo",
    "yoneyama_mai", "shirabi", "neco", "lack", "redjuice",
    "rella", "ryota-h", "so-bin", "tiv", "wada_aruko",
    "modare", "namie", "nardack", "pako", "yoshida_seiji",
    "ciloranko", "dangmill", "infukun", "kuroboshi_kouhaku", "momoco",
    "rumoon", "sheng_he", "tcb", "tomioka_jiro", "tsubata_nozomi",
    "ukumo_uti", "yam_ko", "zumi", "asagi_tosaka", "swd3e2",
    "gyeong", "hxxg", "m_da_s_tarou", "alchemaniac", "anmi",
    "ask_(askziye)", "chen_bin", "dante_wont_die", "dishwasher1510", "dmyo",
    "goomrrat", "haori_iori", "hiten_(hitenkei)", "hoshina_(kuzu-kago)", "krenz_cushart"
]

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

UA = {"User-Agent": "ZAnimeArtBot/2.0"}

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
        <div class="tag">Tracking {len(ARTISTS)} Premium Artists</div>
        <div class="tag" style="margin-top: 8px;">Channel: {CHANNEL_USERNAME}</div>
    </div>
</body>
</html>"""

async def web_index(request):
    return web.Response(text=HTML_PAGE, content_type="text/html")


# ─────────────────────────────────────────────
# Tag Cleaning & Formatting Helpers
# ─────────────────────────────────────────────
def make_hashtag(tag: str) -> str:
    """Removes brackets, splits by underscore, formats to CamelCase Hashtag."""
    if not tag or tag.lower() == "unknown":
        return ""
    # 1. Strip brackets and everything inside them e.g. "ask_(askziye)" -> "ask_"
    tag = re.sub(r'\(.*?\)', '', tag)
    # 2. Split by underscore, capitalize each word for readability
    parts =[p.capitalize() for p in tag.split('_') if p]
    joined = "".join(parts)
    # 3. Strip any remaining special characters
    clean = re.sub(r'[^a-zA-Z0-9]', '', joined)
    return f"#{clean}" if clean else ""

def format_post_data(post: dict) -> tuple:
    """Extracts and formats details into a polished caption."""
    file_url = post.get("file_url")
    large_file_url = post.get("large_file_url", file_url)

    # 1. Extract raw strings
    artist_raw = post.get("tag_string_artist", "Unknown").split()[0]
    character_raw = post.get("tag_string_character", "Original").split()[0]
    
    # 2. Clean display names (remove underscores)
    artist_name = re.sub(r'\(.*?\)', '', artist_raw).replace("_", " ").strip().title()
    character_name = re.sub(r'\(.*?\)', '', character_raw).replace("_", " ").strip().title()

    # 3. Artist Source Link
    source_url = post.get("source")
    if not source_url or not source_url.startswith("http"):
        post_id = post.get("id")
        source_url = f"https://danbooru.donmai.us/posts/{post_id}"

    # 4. Clean Hashtags (Max 3)
    ht_artist = make_hashtag(artist_raw)
    ht_character = make_hashtag(character_raw)
    
    tags =[]
    if ht_artist and ht_artist != "#Unknown": tags.append(ht_artist)
    if ht_character and ht_character != "#Original": tags.append(ht_character)
    tags.append("#AnimeArt")
    
    hashtags_str = " ".join(tags[:3]) # Enforce max 3 tags

    # 5. Build Caption
    caption = (
        f"🎨 <b>Artist:</b> <a href='{source_url}'>{artist_name}</a>\n"
        f"👤 <b>Character:</b> {character_name}\n\n"
        f"{hashtags_str}\n\n"
        f"✨ <b>Join us:</b> {CHANNEL_USERNAME}"
    )
    
    return file_url, large_file_url, caption


# ─────────────────────────────────────────────
# Danbooru Fetcher
# ─────────────────────────────────────────────
async def fetch_random_danbooru_post(search_tag: str) -> dict:
    """Fetches up to 10 random posts and returns the first one that is NOT a duplicate."""
    # We fetch a batch of 10 to easily skip duplicates
    url = f"https://danbooru.donmai.us/posts.json?tags={search_tag}+rating:general&random=true&limit=10"
    
    async with aiohttp.ClientSession(headers=UA) as session:
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, list):
                        for post in data:
                            post_id = str(post.get("id"))
                            # Return the first post that has a file and isn't a duplicate
                            if "file_url" in post and post_id not in SEEN_IDS:
                                return post
                else:
                    logger.error(f"Danbooru API Error: {response.status}")
        except Exception as e:
            logger.error(f"Failed to fetch image: {e}")
    return {}


# ─────────────────────────────────────────────
# Scheduled Job
# ─────────────────────────────────────────────
async def auto_post_job(context: ContextTypes.DEFAULT_TYPE):
    """Fetches, posts the image, and logs the ID to avoid duplicates."""
    target_artist = random.choice(ARTISTS)
    logger.info(f"Fetching new art for randomly chosen artist: {target_artist}...")
    
    post = await fetch_random_danbooru_post(target_artist)
    if not post:
        logger.warning(f"No valid/unique post found for {target_artist}. Skipping this cycle.")
        return

    post_id = str(post.get("id"))
    file_url, large_file_url, caption = format_post_data(post)

    try:
        # 1. Send Broadcast Photo
        await context.bot.send_photo(
            chat_id=CHANNEL_USERNAME,
            photo=large_file_url,
            caption=caption,
            parse_mode=ParseMode.HTML
        )
        
        # 2. Send Uncompressed File Document
        await context.bot.send_document(
            chat_id=CHANNEL_USERNAME,
            document=file_url,
            caption="📁 <b>Full Quality Source</b>",
            parse_mode=ParseMode.HTML
        )
        
        # 3. Add to Database & Sync with Log Group
        SEEN_IDS.add(post_id)
        
        # Create an in-memory text file containing all IDs separated by commas
        ids_string = ",".join(SEEN_IDS)
        file_bytes = io.BytesIO(ids_string.encode('utf-8'))
        file_bytes.name = "posted_ids_database.txt"

        log_caption = (
            f"✅ <b>New Image Posted</b>\n"
            f"ID: <code>{post_id}</code>\n"
            f"Artist tag: {target_artist}\n\n"
            f"📦 <i>Attached is the updated database of all {len(SEEN_IDS)} unique IDs posted so far.</i>"
        )

        await context.bot.send_document(
            chat_id=LOG_GROUP_ID,
            document=file_bytes,
            caption=log_caption,
            parse_mode=ParseMode.HTML
        )
        
        logger.info(f"Successfully posted {post_id} to channel and synced DB!")
        
    except Exception as e:
        logger.error(f"Error posting to Telegram: {e}")


# ─────────────────────────────────────────────
# Main Application Runtime
# ─────────────────────────────────────────────
async def main():
    if not BOT_TOKEN:
        logger.error("TELEGRAM_TOKEN is missing! Set it in your environment variables.")
        return

    # Initialize Telegram App
    app = Application.builder().token(BOT_TOKEN).build()
    
    job_queue = app.job_queue
    logger.info(f"Scheduling auto-post every {POST_INTERVAL} seconds...")
    job_queue.run_repeating(auto_post_job, interval=POST_INTERVAL, first=10)

    # Initialize Web Server for Render Health checks
    web_app = web.Application()
    web_app.router.add_get("/", web_index)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    
    await site.start()
    logger.info(f"🚀 Web Server started on port {PORT}")
    
    # Start the app and job queue (WITHOUT POLLING)
    await app.initialize()
    await app.start()
    logger.info("🤖 Bot is now active! Broadcast jobs are running.")

    # Graceful exit handling
    stop_signal = asyncio.Event()
    loop = asyncio.get_running_loop()
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_signal.set)

    await stop_signal.wait()

    logger.info("Shutting down...")
    await app.stop()
    await app.shutdown()
    await site.stop()
    await runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
