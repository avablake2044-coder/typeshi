import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import os
import io
import time
import logging
import asyncio
import zipfile
from datetime import datetime
import urllib.parse

# ─────────────────────────────────────────────
# Third-party Imports
# ─────────────────────────────────────────────
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, PicklePersistence, filters
)

# Web Server Imports (For the Render Status Page)
from aiohttp import web

# ─────────────────────────────────────────────
# Logging Configuration
# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# HTML LANDING PAGE (The "Website" for Render)
# ─────────────────────────────────────────────
HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Omega Manga Bot - Status</title>
    <style>
        body { background-color: #0f172a; color: #e2e8f0; font-family: sans-serif; display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100vh; margin: 0; }
        .container { background: #1e293b; padding: 40px; border-radius: 12px; text-align: center; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }
        h1 { color: #38bdf8; }
        .status { color: #4ade80; font-weight: bold; margin: 20px 0; }
        a { background: #38bdf8; color: #0f172a; padding: 10px 20px; text-decoration: none; border-radius: 6px; font-weight: bold; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Omega Manga Bot</h1>
        <div class="status">🟢 All Systems Operational</div>
        <p>The Telegram Webhook and Bot are actively running.</p>
        <br>
        <a href="https://t.me/YOUR_BOT_USERNAME">Open Bot in Telegram</a>
    </div>
</body>
</html>
"""

# ─────────────────────────────────────────────
# API Fetcher Class (Primary + 2 Backups)
# ─────────────────────────────────────────────
class MangaFetcher:
    def __init__(self):
        self.session = None

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    # --- 1. SEARCHING & TOP MANHWA ---
    async def get_top_manga(self):
        """Fetches top manga using MangaDex as primary."""
        session = await self.get_session()
        try:
            # Primary: MangaDex
            url = "https://api.mangadex.org/manga?includes[]=cover_art&order[followedCount]=desc&limit=5&contentRating[]=safe"
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results =[]
                    for item in data['data']:
                        title = item['attributes']['title'].get('en', 'Unknown Title')
                        results.append({'id': item['id'], 'title': title, 'source': 'mdex'})
                    return results
        except Exception as e:
            logger.error(f"Primary API (MangaDex) failed: {e}")
            
        # Fallbacks would go here if MangaDex fails to load top manga
        return[]

    async def search_manga(self, query: str):
        """Searches with Primary, falls back to Backup 1, then Backup 2."""
        session = await self.get_session()
        
        # PRIMARY: MangaDex
        try:
            url = f"https://api.mangadex.org/manga?title={urllib.parse.quote(query)}&limit=5&order[relevance]=desc"
            async with session.get(url, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data['data']:
                        return[{'id': m['id'], 'title': m['attributes']['title'].get('en', 'Unknown'), 'source': 'mdex'} for m in data['data']]
        except Exception as e:
            logger.warning(f"MangaDex Search failed: {e}")

        # BACKUP 1: Comick
        try:
            url = f"https://api.comick.cc/v1.0/search?q={urllib.parse.quote(query)}&limit=5"
            async with session.get(url, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data:
                        return [{'id': m['hid'], 'title': m['title'], 'source': 'comick'} for m in data]
        except Exception as e:
            logger.warning(f"Comick Search failed: {e}")

        # BACKUP 2: Consumet API (Placeholder example endpoint)
        try:
            url = f"https://api.consumet.org/manga/mangadex/{urllib.parse.quote(query)}"
            async with session.get(url, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('results'):
                        return [{'id': m['id'], 'title': m['title'], 'source': 'consumet'} for m in data['results'][:5]]
        except Exception as e:
            logger.warning(f"Consumet Search failed: {e}")

        return[] # All APIs failed or no results

    # --- 2. MANGA DETAILS ---
    async def get_manga_details(self, manga_id: str, source: str):
        session = await self.get_session()
        if source == 'mdex':
            url = f"https://api.mangadex.org/manga/{manga_id}?includes[]=cover_art"
            async with session.get(url) as resp:
                data = (await resp.json())['data']
                desc = data['attributes']['description'].get('en', 'No description.')
                return desc[:800] + "..." if len(desc) > 800 else desc
        elif source == 'comick':
            url = f"https://api.comick.cc/comic/{manga_id}"
            async with session.get(url) as resp:
                data = await resp.json()
                desc = data['comic'].get('desc', 'No description.')
                return desc[:800] + "..." if len(desc) > 800 else desc
        return "Description unavailable."

    # --- 3. CHAPTER LISTING ---
    async def get_chapters(self, manga_id: str, source: str, offset: int = 0):
        session = await self.get_session()
        limit = 10
        if source == 'mdex':
            url = f"https://api.mangadex.org/manga/{manga_id}/feed?translatedLanguage[]=en&order[chapter]=desc&limit={limit}&offset={offset}"
            async with session.get(url) as resp:
                data = await resp.json()
                chapters =[{'id': c['id'], 'num': c['attributes'].get('chapter', '?')} for c in data['data']]
                return chapters, data['total'] > (offset + limit)
        elif source == 'comick':
            url = f"https://api.comick.cc/comic/{manga_id}/chapters?lang=en&limit={limit}&page={int(offset/limit)+1}"
            async with session.get(url) as resp:
                data = await resp.json()
                chapters = [{'id': c['hid'], 'num': c.get('chap', '?')} for c in data['chapters']]
                return chapters, len(data['chapters']) == limit
        return[], False

    # --- 4. IMAGE EXTRACTION ---
    async def get_chapter_images(self, chapter_id: str, source: str):
        session = await self.get_session()
        if source == 'mdex':
            url = f"https://api.mangadex.org/at-home/server/{chapter_id}"
            async with session.get(url) as resp:
                data = await resp.json()
                base = data['baseUrl']
                hash_id = data['chapter']['hash']
                return [f"{base}/data/{hash_id}/{img}" for img in data['chapter']['data']]
        elif source == 'comick':
            url = f"https://api.comick.cc/chapter/{chapter_id}"
            async with session.get(url) as resp:
                data = await resp.json()
                return [img['url'] for img in data['chapter']['images']]
        return[]

fetcher = MangaFetcher()

# ─────────────────────────────────────────────
# Bot Handlers
# ─────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔄 Fetching top trending Manhwa/Manga...")
    top_mangas = await fetcher.get_top_manga()
    
    text = "👋 *Welcome to Omega Manga Bot!*\n\n🔍 Type any Manga/Manhwa name to search.\n\n🔥 *Top Trending right now:*"
    keyboard = []
    for m in top_mangas:
        keyboard.append([InlineKeyboardButton(m['title'], callback_data=f"manga|{m['source']}|{m['id']}")])
    
    await msg.edit_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    msg = await update.message.reply_text(f"🔍 Searching for `{query}` across multiple sources...", parse_mode='Markdown')
    
    results = await fetcher.search_manga(query)
    
    if not results:
        await msg.edit_text("❌ No results found. Try a different name.")
        return
        
    keyboard =[]
    for m in results:
        keyboard.append([InlineKeyboardButton(m['title'], callback_data=f"manga|{m['source']}|{m['id']}")])
        
    await msg.edit_text("✅ *Results Found:*\nSelect one to view chapters:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split('|')
    action = data[0]

    # --- SHOW MANGA DETAILS & CHAPTERS ---
    if action == 'manga':
        source, manga_id = data[1], data[2]
        await query.edit_message_text("⏳ Loading description and chapters...")
        
        desc = await fetcher.get_manga_details(manga_id, source)
        chapters, has_next = await fetcher.get_chapters(manga_id, source, 0)
        
        text = f"📖 *Description:*\n{desc}\n\n📚 *Select a Chapter:*"
        keyboard = []
        for ch in chapters:
            keyboard.append([InlineKeyboardButton(f"Chapter {ch['num']}", callback_data=f"dl|{source}|{ch['id']}")])
            
        if has_next:
            keyboard.append([InlineKeyboardButton("Next Page ➡️", callback_data=f"page|{source}|{manga_id}|10")])
            
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    # --- PAGINATION ---
    elif action == 'page':
        source, manga_id, offset = data[1], data[2], int(data[3])
        chapters, has_next = await fetcher.get_chapters(manga_id, source, offset)
        
        keyboard =[]
        for ch in chapters:
            keyboard.append([InlineKeyboardButton(f"Chapter {ch['num']}", callback_data=f"dl|{source}|{ch['id']}")])
            
        nav_row =[]
        if offset > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page|{source}|{manga_id}|{max(0, offset-10)}"))
        if has_next:
            nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"page|{source}|{manga_id}|{offset+10}"))
            
        if nav_row:
            keyboard.append(nav_row)
            
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

    # --- DOWNLOAD & SEND CHAPTER ---
    elif action == 'dl':
        source, chapter_id = data[1], data[2]
        status_msg = await query.message.reply_text("📥 Extracting images... Please wait.")
        
        image_urls = await fetcher.get_chapter_images(chapter_id, source)
        if not image_urls:
            await status_msg.edit_text("❌ Failed to fetch chapter images. They might be paywalled or unavailable.")
            return

        await status_msg.edit_text(f"⏳ Downloading {len(image_urls)} images...")
        
        # Download images concurrently into a zip file in memory
        session = await fetcher.get_session()
        zip_buffer = io.BytesIO()
        zip_buffer.name = f"Chapter_{chapter_id}.cbz" # CBZ is standard comic book format
        
        async def fetch_image(idx, url):
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return idx, await resp.read()
            except:
                pass
            return idx, None

        tasks =[fetch_image(i, url) for i, url in enumerate(image_urls)]
        results = await asyncio.gather(*tasks)
        
        with zipfile.ZipFile(zip_buffer, 'w') as zipf:
            for idx, img_data in sorted(results):
                if img_data:
                    zipf.writestr(f"{idx:03d}.jpg", img_data)

        zip_buffer.seek(0)
        
        await status_msg.edit_text("📤 Uploading chapter to Telegram...")
        await context.bot.send_document(
            chat_id=query.message.chat_id, 
            document=zip_buffer,
            caption="✅ Here is your chapter!\n\n*(Note: .cbz files can be opened by comic readers or by renaming to .zip)*",
            parse_mode='Markdown'
        )
        await status_msg.delete()


# ─────────────────────────────────────────────
# MAIN EXECUTION (Render Webhook Setup)
# ─────────────────────────────────────────────

async def main():
    TOKEN = os.environ.get("TELEGRAM_TOKEN")
    if not TOKEN:
        logger.error("❌ No valid bot token found.")
        return

    PORT = int(os.environ.get("PORT", "8443"))
    RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
    
    # Setup Bot
    persistence = PicklePersistence(filepath="bot_data.pkl")
    application = Application.builder().token(TOKEN).persistence(persistence).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(CallbackQueryHandler(button_handler))

    # Define Web Server Routes
    async def index_handler(request):
        return web.Response(text=HTML_PAGE, content_type='text/html')

    async def webhook_handler(request):
        try:
            json_data = await request.json()
            update = Update.de_json(json_data, application.bot)
            await application.update_queue.put(update)
            return web.Response()
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            return web.Response(status=500)

    # Boot Process
    if RENDER_EXTERNAL_URL:
        await application.initialize()
        await application.start()
        
        webhook_url = f"{RENDER_EXTERNAL_URL}/{TOKEN}"
        logger.info(f"🌐 Setting webhook to: {webhook_url}")
        await application.bot.set_webhook(url=webhook_url)

        web_app = web.Application()
        web_app.router.add_get('/', index_handler)
        web_app.router.add_post(f'/{TOKEN}', webhook_handler)

        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        
        logger.info(f"🚀 Server started on port {PORT}")
        
        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            if fetcher.session:
                await fetcher.session.close()
            await application.stop()
            await application.shutdown()
            await site.stop()
            await runner.cleanup()
    else:
        logger.info("🔄 Starting polling mode (Local)")
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        
        stop_event = asyncio.Event()
        await stop_event.wait()
        
        if fetcher.session:
            await fetcher.session.close()
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
