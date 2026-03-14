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
POST_INTERVAL = 90 # 1 hour in seconds (Set to 3600 for 1 hour, 90 is 1.5 mins for testing)

# Memory to track duplicates
SEEN_IDS = set()

# 100 Verified High-Quality Artists (200+ posts, SFW-leaning, no bracket aliases)
ARTISTS =[
    "wlop", "mika_pikazo", "rurudo", "yoneyama_mai", "shirabi",
    "neco", "lack", "redjuice", "rella", "ryota-h",
    "so-bin", "tiv", "wada_aruko", "namie", "nardack",
    "pako", "yoshida_seiji", "kuroboshi_kouhaku", "momoco", "tcb",
    "ukumo_uti", "swd3e2", "hxxg", "alchemaniac", "anmi",
    "dante_wont_die", "dishwasher1510", "goomrrat", "krenz_cushart", "kantoku",
    "kurone_mishima", "huke", "misaki_kurehito", "abec", "liduke",
    "ciloranko", "fuzichoco", "vofan", "loundraw", "kawacy",
    "saitom", "shunya_yamashita", "mignon", "shigure_ui", "ito_noizi",
    "tsunako", "namori", "takeuchi_takashi", "koyama_hirokazu", "nanao_naru",
    "mitsumi_misato", "azuru", "parsley", "shizuma_yoshinori", "ugume",
    "sakura_koharu", "hisasi", "naoki_saito", "nishizawa_5-miri", "ponkan8",
    "ukai_saki", "harada_takehito", "soejima_shigenori", "yasuda_suzuhito", "himesuz",
    "ishikei", "yd", "popman3580", "kakage", "kagura_nana",
    "ideolo", "cierra", "koyoriin", "gemi", "tomose_shunsaku",
    "yomu", "daito", "kuwashima_rein", "shibafu", "kincora",
    "m_da_s_tarou", "paryi", "ninomoto_nino", "mikimoto_haruhiko", "urushihara_satoshi",
    "yam_ko", "ryou_kameko", "matsuryu", "kikuchi_seiji", "homunculus",
    "sakimichan", "ask", "torino", "hiten", "tomioka_jiro",
    "infukun", "dangmill", "rumoon", "sheng_he", "ke-ta"
]

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

UA = {"User-Agent": "ZAnimeArtBot/3.1"}

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
    tag = re.sub(r'\(.*?\)', '', tag)
    parts = [p.capitalize() for p in tag.split('_') if p]
    joined = "".join(parts)
    clean = re.sub(r'[^a-zA-Z0-9]', '', joined)
    return f"#{clean}" if clean else ""

def format_post_data(post: dict) -> tuple:
    """Extracts and formats details into a polished caption."""
    file_url = post.get("file_url")
    large_file_url = post.get("large_file_url", file_url)

    # 1. Safely extract strings (Danbooru returns "" if empty)
    artist_str = post.get("tag_string_artist", "")
    character_str = post.get("tag_string_character", "")
    
    # 2. Safely get the first tag, or apply a default if it's completely empty
    artist_raw = artist_str.split()[0] if artist_str.strip() else "Unknown"
    character_raw = character_str.split()[0] if character_str.strip() else "Original"

    # 3. Clean display names (remove underscores)
    artist_name = re.sub(r'\(.*?\)', '', artist_raw).replace("_", " ").strip().title()
    character_name = re.sub(r'\(.*?\)', '', character_raw).replace("_", " ").strip().title()

    # 4. Artist Source Link
    source_url = post.get("source")
    if not source_url or not source_url.startswith("http"):
        post_id = post.get("id")
        source_url = f"https://danbooru.donmai.us/posts/{post_id}"

    # 5. Clean Hashtags
    ht_artist = make_hashtag(artist_raw)
    ht_character = make_hashtag(character_raw)
    
    tags =[]
    if ht_artist and ht_artist != "#Unknown": tags.append(ht_artist)
    if ht_character and ht_character != "#Original": tags.append(ht_character)
    tags.append("#AnimeArt")
    
    hashtags_str = " ".join(tags[:3])

    # 6. Build Caption
    caption = (
        f"🎨 <b>Artist:</b> <a href='{source_url}'>{artist_name}</a>\n"
        f"👤 <b>Character:</b> {character_name}\n\n"
        f"{hashtags_str}\n\n"
        f"✨ <b>Join us:</b> {CHANNEL_USERNAME}"
    )
    
    return file_url, large_file_url, caption


# ─────────────────────────────────────────────
# Persistence Loader
# ─────────────────────────────────────────────
async def load_seen_ids(bot):
    """Loads previously saved IDs from the pinned database file in the Log Group."""
    global SEEN_IDS
    try:
        chat = await bot.get_chat(LOG_GROUP_ID)
        if chat.pinned_message and chat.pinned_message.document:
            file_id = chat.pinned_message.document.file_id
            tg_file = await bot.get_file(file_id)
            
            # Download file from telegram
            file_bytes = await tg_file.download_as_bytearray()
            content = file_bytes.decode('utf-8')
            
            if content:
                loaded_ids =[x.strip() for x in content.split(',') if x.strip()]
                SEEN_IDS.update(loaded_ids)
                logger.info(f"✅ Successfully restored {len(SEEN_IDS)} IDs from pinned Telegram database.")
        else:
            logger.info("ℹ️ No pinned database found in log group. Starting completely fresh.")
    except Exception as e:
        logger.error(f"⚠️ Failed to load SEEN_IDS from Telegram. Ensure the bot is an admin in the Log Group! Error: {e}")


# ─────────────────────────────────────────────
# Danbooru Fetcher
# ─────────────────────────────────────────────
async def fetch_random_danbooru_post(search_tag: str) -> dict:
    """Fetches up to 10 random posts and returns the first one that is NOT a duplicate."""
    url = f"https://danbooru.donmai.us/posts.json?tags={search_tag}+rating:general&random=true&limit=10"
    
    async with aiohttp.ClientSession(headers=UA) as session:
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, list):
                        for post in data:
                            post_id = str(post.get("id"))
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
        
        ids_string = ",".join(SEEN_IDS)
        file_bytes = io.BytesIO(ids_string.encode('utf-8'))
        file_bytes.name = "posted_ids_database.txt"

        log_caption = (
            f"✅ <b>New Image Posted</b>\n"
            f"ID: <code>{post_id}</code>\n"
            f"Artist tag: {target_artist}\n\n"
            f"📦 <i>Attached is the updated database of all {len(SEEN_IDS)} unique IDs posted so far.</i>"
        )

        db_message = await context.bot.send_document(
            chat_id=LOG_GROUP_ID,
            document=file_bytes,
            caption=log_caption,
            parse_mode=ParseMode.HTML
        )
        
        # 4. PIN THE NEW DATABASE so the bot can find it on restart
        try:
            await context.bot.unpin_all_chat_messages(chat_id=LOG_GROUP_ID)
            await db_message.pin(disable_notification=True)
            logger.info("📌 Pinned the latest database file for persistence.")
        except Exception as pin_err:
            logger.warning(f"⚠️ Could not pin the DB message. Ensure the bot has 'Pin Messages' admin rights in the log group! Error: {pin_err}")
        
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
    await app.initialize()

    # ---- LOAD DATABASE BEFORE STARTING JOBS ----
    logger.info("Loading previous IDs database from Telegram...")
    await load_seen_ids(app.bot)

    # Start repeating job
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
