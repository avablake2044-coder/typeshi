import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import os
import io
import re
import signal
import logging
import asyncio
import hashlib
import urllib.parse
from html import escape as esc
from collections import OrderedDict

import aiohttp
from PIL import Image
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from aiohttp import web

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
MAX_CB_BYTES = 64
MAX_PDF_MB = 50
DL_SEMAPHORE = asyncio.Semaphore(6)
API_TIMEOUT = aiohttp.ClientTimeout(total=15)
HEADERS = {"User-Agent": "OmegaMangaBot/2.0"}
BOT_USERNAME = os.environ.get("BOT_USERNAME", "OmegaMangaBot")

# ─────────────────────────────────────────────
# Memory-capped dictionaries (prevent leaks)
# ─────────────────────────────────────────────
class CappedDict(OrderedDict):
    """OrderedDict that evicts oldest entries at capacity."""
    def __init__(self, capacity: int = 10_000):
        super().__init__()
        self._cap = capacity

    def __setitem__(self, key, value):
        if key not in self and len(self) >= self._cap:
            self.popitem(last=False)
        super().__setitem__(key, value)


_id_map = CappedDict(20_000)        # short hash → original id
_manga_meta = CappedDict(5_000)     # manga short id → {title, source, cover}
_chapter_meta = CappedDict(20_000)  # chapter short id → {num, manga_title}


def shorten(original: str) -> str:
    if len(original) <= 12:
        return original
    short = hashlib.md5(original.encode()).hexdigest()[:12]
    _id_map[short] = original
    return short


def resolve(short: str) -> str:
    return _id_map.get(short, short)


def sanitize_fn(name: str) -> str:
    """Remove filesystem-unsafe chars, cap length."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip()[:80]


# ─────────────────────────────────────────────
# MangaDex helpers — title & cover extraction
# ─────────────────────────────────────────────
# THIS IS THE KEY FIX for "Unknown Title".
# MangaDex stores titles in the ORIGINAL language.
# Korean manhwa have {"ko": "..."} with English
# buried inside altTitles.

def extract_title(item: dict) -> str:
    """
    Try, in order:
      1. Main title dict → en, ja-ro, ko-ro, zh-ro
      2. altTitles list  → en
      3. altTitles list  → any romanized
      4. First available value anywhere
    """
    attrs = item.get("attributes", {})
    titles = attrs.get("title", {})
    alts = attrs.get("altTitles", [])

    # ── 1. preferred languages in main title ──
    for lang in ("en", "ja-ro", "ko-ro", "zh-ro"):
        if titles.get(lang):
            return titles[lang]

    # ── 2. English in altTitles ──
    for alt in alts:
        if isinstance(alt, dict) and alt.get("en"):
            return alt["en"]

    # ── 3. Romanized in altTitles ──
    for alt in alts:
        if isinstance(alt, dict):
            for lang in ("ja-ro", "ko-ro", "zh-ro"):
                if alt.get(lang):
                    return alt[lang]

    # ── 4. Anything at all ──
    if titles:
        return next(iter(titles.values()))
    for alt in alts:
        if isinstance(alt, dict) and alt:
            return next(iter(alt.values()))

    return "Unknown Title"


def extract_cover(item: dict) -> str | None:
    """Pull the 512px cover thumbnail URL from includes."""
    for rel in item.get("relationships", []):
        if rel.get("type") == "cover_art":
            fn = rel.get("attributes", {}).get("fileName")
            if fn:
                mid = item["id"]
                return (
                    f"https://uploads.mangadex.org"
                    f"/covers/{mid}/{fn}.512.jpg"
                )
    return None


# ─────────────────────────────────────────────
# PDF Builder (replaces CBZ/ZIP)
# ─────────────────────────────────────────────
def images_to_pdf(
    raw_pages: list[bytes], filename: str
) -> io.BytesIO | None:
    """Convert a list of image bytes into a single PDF."""
    pil_imgs = []
    for raw in raw_pages:
        if raw is None:
            continue
        try:
            img = Image.open(io.BytesIO(raw))
            if img.mode != "RGB":
                img = img.convert("RGB")
            pil_imgs.append(img)
        except Exception:
            continue

    if not pil_imgs:
        return None

    buf = io.BytesIO()
    if len(pil_imgs) == 1:
        pil_imgs[0].save(buf, "PDF")
    else:
        pil_imgs[0].save(
            buf, "PDF", save_all=True, append_images=pil_imgs[1:]
        )
    buf.seek(0)
    buf.name = filename
    return buf


# ─────────────────────────────────────────────
# HTML Landing Page
# ─────────────────────────────────────────────
HTML_PAGE = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Omega Manga Bot</title>
<style>
body{{background:#0f172a;color:#e2e8f0;font-family:system-ui,
sans-serif;display:flex;align-items:center;justify-content:center;
height:100vh;margin:0}}
.c{{background:#1e293b;padding:48px;border-radius:16px;
text-align:center;box-shadow:0 20px 40px rgba(0,0,0,.5);
max-width:420px}}
h1{{color:#38bdf8;margin:0 0 8px}}
.s{{color:#4ade80;font-weight:700;font-size:1.1em;margin:16px 0}}
p{{color:#94a3b8;line-height:1.6}}
a{{display:inline-block;background:#38bdf8;color:#0f172a;
padding:12px 28px;text-decoration:none;border-radius:8px;
font-weight:700;margin-top:12px}}
</style></head>
<body><div class="c">
<h1>🌀 Omega Manga Bot</h1>
<div class="s">🟢 Online</div>
<p>Search, browse &amp; download manga chapters as PDF
— directly in Telegram.</p>
<a href="https://t.me/{BOT_USERNAME}">Open in Telegram</a>
</div></body></html>"""


# ─────────────────────────────────────────────
# MangaFetcher — Primary + 2 Fallbacks
# ─────────────────────────────────────────────
class MangaFetcher:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None

    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=API_TIMEOUT, headers=HEADERS
            )
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    # ── Normalize source ──────────────────────
    @staticmethod
    def _norm(source: str) -> str:
        # consumet wraps MangaDex IDs
        return "mdex" if source == "consumet" else source

    # ── TOP MANGA ─────────────────────────────
    async def get_top_manga(self) -> list[dict]:
        session = await self.get_session()
        try:
            url = (
                "https://api.mangadex.org/manga"
                "?includes[]=cover_art"
                "&order[followedCount]=desc"
                "&limit=5"
                "&contentRating[]=safe"
                "&contentRating[]=suggestive"
                "&availableTranslatedLanguage[]=en"
            )
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return [
                    {
                        "id": m["id"],
                        "title": extract_title(m),
                        "cover": extract_cover(m),
                        "source": "mdex",
                    }
                    for m in data.get("data", [])
                ]
        except Exception as e:
            logger.error(f"Top manga: {e}")
        return []

    # ── SEARCH ────────────────────────────────
    async def search_manga(self, query: str) -> list[dict]:
        session = await self.get_session()
        enc = urllib.parse.quote(query)

        # PRIMARY — MangaDex
        try:
            url = (
                f"https://api.mangadex.org/manga"
                f"?title={enc}&limit=5"
                f"&order[relevance]=desc"
                f"&includes[]=cover_art"
                f"&availableTranslatedLanguage[]=en"
            )
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("data"):
                        return [
                            {
                                "id": m["id"],
                                "title": extract_title(m),
                                "cover": extract_cover(m),
                                "source": "mdex",
                            }
                            for m in data["data"]
                        ]
        except Exception as e:
            logger.warning(f"MangaDex search: {e}")

        # BACKUP 1 — Comick
        try:
            url = (
                f"https://api.comick.fun/v1.0/search"
                f"?q={enc}&limit=5"
            )
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data:
                        return [
                            {
                                "id": m["hid"],
                                "title": m.get("title", "Unknown"),
                                "cover": None,
                                "source": "comick",
                            }
                            for m in data
                        ]
        except Exception as e:
            logger.warning(f"Comick search: {e}")

        # BACKUP 2 — Consumet
        try:
            url = (
                f"https://api.consumet.org/manga"
                f"/mangadex/{enc}"
            )
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("results"):
                        return [
                            {
                                "id": m["id"],
                                "title": m.get("title", "Unknown"),
                                "cover": m.get("image"),
                                "source": "consumet",
                            }
                            for m in data["results"][:5]
                        ]
        except Exception as e:
            logger.warning(f"Consumet search: {e}")

        return []

    # ── DETAILS ───────────────────────────────
    async def get_manga_details(
        self, manga_id: str, source: str
    ) -> dict:
        source = self._norm(source)
        session = await self.get_session()
        out = {
            "description": "No description available.",
            "cover_url": None,
            "title": "Unknown",
        }
        try:
            if source == "mdex":
                url = (
                    f"https://api.mangadex.org/manga"
                    f"/{manga_id}?includes[]=cover_art"
                )
                async with session.get(url) as resp:
                    if resp.status == 200:
                        item = (await resp.json())["data"]
                        out["title"] = extract_title(item)
                        out["description"] = (
                            item["attributes"]["description"]
                            .get("en", "No description.")
                        )
                        out["cover_url"] = extract_cover(item)

            elif source == "comick":
                url = (
                    f"https://api.comick.fun/comic/{manga_id}"
                )
                async with session.get(url) as resp:
                    if resp.status == 200:
                        comic = (await resp.json()).get(
                            "comic", {}
                        )
                        out["title"] = comic.get(
                            "title", "Unknown"
                        )
                        out["description"] = comic.get(
                            "desc", "No description."
                        )
                        covers = comic.get("md_covers", [])
                        if covers:
                            b2 = covers[0].get("b2key", "")
                            if b2:
                                out["cover_url"] = (
                                    f"https://meo.comick.pictures"
                                    f"/{b2}"
                                )
        except Exception as e:
            logger.error(f"Details error: {e}")
        return out

    # ── CHAPTERS ──────────────────────────────
    async def get_chapters(
        self, manga_id: str, source: str, offset: int = 0
    ) -> tuple[list[dict], bool]:
        source = self._norm(source)
        session = await self.get_session()
        limit = 10
        try:
            if source == "mdex":
                url = (
                    f"https://api.mangadex.org/manga"
                    f"/{manga_id}/feed"
                    f"?translatedLanguage[]=en"
                    f"&order[chapter]=desc"
                    f"&limit={limit}&offset={offset}"
                )
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return [], False
                    data = await resp.json()
                    chs = [
                        {
                            "id": c["id"],
                            "num": c["attributes"].get(
                                "chapter", "?"
                            ),
                        }
                        for c in data.get("data", [])
                    ]
                    total = data.get("total", 0)
                    return chs, total > offset + limit

            elif source == "comick":
                page = offset // limit + 1
                url = (
                    f"https://api.comick.fun/comic"
                    f"/{manga_id}/chapters"
                    f"?lang=en&limit={limit}&page={page}"
                )
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return [], False
                    data = await resp.json()
                    raw = data.get("chapters", [])
                    chs = [
                        {
                            "id": c["hid"],
                            "num": c.get("chap", "?"),
                        }
                        for c in raw
                    ]
                    return chs, len(raw) == limit

        except Exception as e:
            logger.error(f"Chapters error: {e}")
        return [], False

    # ── IMAGES ────────────────────────────────
    async def get_chapter_images(
        self, chapter_id: str, source: str
    ) -> list[str]:
        source = self._norm(source)
        session = await self.get_session()
        try:
            if source == "mdex":
                url = (
                    f"https://api.mangadex.org/at-home"
                    f"/server/{chapter_id}"
                )
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()
                    base = data["baseUrl"]
                    h = data["chapter"]["hash"]
                    return [
                        f"{base}/data/{h}/{img}"
                        for img in data["chapter"]["data"]
                    ]

            elif source == "comick":
                url = (
                    f"https://api.comick.fun/chapter"
                    f"/{chapter_id}"
                )
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()
                    return [
                        i["url"]
                        for i in data.get("chapter", {}).get(
                            "images", []
                        )
                    ]
        except Exception as e:
            logger.error(f"Images error: {e}")
        return []


fetcher = MangaFetcher()


# ─────────────────────────────────────────────
# Telegram Handlers
# ─────────────────────────────────────────────

async def start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    msg = await update.message.reply_text("⏳ Loading…")
    top = await fetcher.get_top_manga()

    text = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "    🌀  <b>OMEGA MANGA BOT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send any title to search &amp; download\n"
        "chapters as <b>PDF</b> instantly.\n\n"
        "🔥  <b>Trending Now</b>\n"
        "─────────────────────"
    )

    kb = []
    for i, m in enumerate(top, 1):
        sid = shorten(m["id"])
        _manga_meta[sid] = {
            "title": m["title"],
            "source": m["source"],
            "cover": m.get("cover"),
        }
        cb = f"m|{m['source']}|{sid}"
        if len(cb.encode()) <= MAX_CB_BYTES:
            kb.append(
                [
                    InlineKeyboardButton(
                        f"{i}.  {m['title']}",
                        callback_data=cb,
                    )
                ]
            )

    await msg.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def help_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "    ❓  <b>HOW TO USE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "1️⃣  Type any manga / manhwa name\n"
        "2️⃣  Select a result from the list\n"
        "3️⃣  Tap a chapter to download\n"
        "4️⃣  Receive it as a <b>PDF</b> file\n\n"
        "📌  <b>Sources:</b>  MangaDex · Comick\n"
        "📎  <b>Format:</b>  "
        "<code>Title - Chapter X.pdf</code>",
        parse_mode="HTML",
    )


async def handle_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    q = update.message.text.strip()
    if not q:
        return

    msg = await update.message.reply_text(
        f"🔍  Searching  <b>{esc(q)}</b> …",
        parse_mode="HTML",
    )

    results = await fetcher.search_manga(q)

    if not results:
        await msg.edit_text(
            "❌  <b>No results found.</b>\n\n"
            "<i>Try a different spelling or "
            "the original title.</i>",
            parse_mode="HTML",
        )
        return

    kb = []
    for m in results:
        sid = shorten(m["id"])
        _manga_meta[sid] = {
            "title": m["title"],
            "source": m["source"],
            "cover": m.get("cover"),
        }
        cb = f"m|{m['source']}|{sid}"
        if len(cb.encode()) <= MAX_CB_BYTES:
            kb.append(
                [
                    InlineKeyboardButton(
                        f"📖  {m['title']}",
                        callback_data=cb,
                    )
                ]
            )

    await msg.edit_text(
        f"✅  <b>Results for</b>  \"{esc(q)}\"\n"
        "─────────────────────\n"
        "Tap a title to view chapters:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ─────────────────────────────────────────────
# Callback Router
# ─────────────────────────────────────────────

async def button_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|")
    action = parts[0]

    try:
        # ── View manga details ────────────────
        if action == "m" and len(parts) == 3:
            source, sid = parts[1], parts[2]
            manga_id = resolve(sid)
            meta = _manga_meta.get(sid, {})
            stored_title = meta.get("title", "Unknown")
            chat_id = query.message.chat_id

            # delete old message (search results)
            try:
                await query.message.delete()
            except Exception:
                pass

            loading = await context.bot.send_message(
                chat_id,
                "⏳  Loading details …",
            )

            details, (chapters, has_next) = (
                await asyncio.gather(
                    fetcher.get_manga_details(
                        manga_id, source
                    ),
                    fetcher.get_chapters(
                        manga_id, source, 0
                    ),
                )
            )

            title = details.get("title") or stored_title
            desc = details.get(
                "description", "No description."
            )
            cover = (
                details.get("cover_url")
                or meta.get("cover")
            )

            # update stored meta
            _manga_meta[sid] = {
                "title": title,
                "source": source,
                "cover": cover,
            }

            # truncate description for caption
            if len(desc) > 450:
                desc = desc[:450] + "…"

            caption = (
                f"📖  <b>{esc(title)}</b>\n"
                f"{'━' * 24}\n\n"
                f"{esc(desc)}\n\n"
                f"📚  <b>Chapters</b>  —  "
                f"tap to download PDF"
            )

            keyboard = _build_chapter_kb(
                chapters, source, manga_id, title,
                0, has_next,
            )

            await loading.delete()

            # try sending with cover image
            if cover:
                try:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=cover,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(
                            keyboard
                        ),
                    )
                    return
                except Exception as e:
                    logger.warning(
                        f"Cover photo failed: {e}"
                    )

            # fallback: plain text
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        # ── Chapter pagination ────────────────
        elif action == "p" and len(parts) == 4:
            source, sid = parts[1], parts[2]
            offset = int(parts[3])
            manga_id = resolve(sid)
            meta = _manga_meta.get(sid, {})
            title = meta.get("title", "Unknown")

            chapters, has_next = await fetcher.get_chapters(
                manga_id, source, offset
            )
            keyboard = _build_chapter_kb(
                chapters, source, manga_id, title,
                offset, has_next,
            )
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        # ── Download chapter as PDF ───────────
        elif action == "d" and len(parts) == 3:
            source, ch_sid = parts[1], parts[2]
            chapter_id = resolve(ch_sid)
            meta = _chapter_meta.get(ch_sid, {})
            manga_title = meta.get(
                "manga_title", "Manga"
            )
            chapter_num = meta.get("num", "0")

            await _download_pdf(
                query, context,
                chapter_id, source,
                manga_title, chapter_num,
            )

    except Exception as e:
        logger.error(
            f"button_handler: {e}", exc_info=True
        )
        try:
            await query.message.reply_text(
                "⚠️  Something went wrong. Try again."
            )
        except Exception:
            pass


# ─────────────────────────────────────────────
# Chapter keyboard builder (2 per row)
# ─────────────────────────────────────────────

def _build_chapter_kb(
    chapters: list[dict],
    source: str,
    manga_id: str,
    manga_title: str,
    offset: int,
    has_next: bool,
) -> list[list[InlineKeyboardButton]]:
    m_sid = shorten(manga_id)
    kb: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for ch in chapters:
        ch_sid = shorten(ch["id"])
        _chapter_meta[ch_sid] = {
            "num": ch["num"],
            "manga_title": manga_title,
        }
        cb = f"d|{source}|{ch_sid}"
        if len(cb.encode()) > MAX_CB_BYTES:
            continue

        row.append(
            InlineKeyboardButton(
                f"📄 Ch. {ch['num']}", callback_data=cb
            )
        )
        if len(row) == 2:          # ← 2 buttons per row
            kb.append(row)
            row = []

    if row:
        kb.append(row)

    # navigation row
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        cb = f"p|{source}|{m_sid}|{max(0, offset - 10)}"
        if len(cb.encode()) <= MAX_CB_BYTES:
            nav.append(
                InlineKeyboardButton(
                    "⬅️ Prev", callback_data=cb
                )
            )
    if has_next:
        cb = f"p|{source}|{m_sid}|{offset + 10}"
        if len(cb.encode()) <= MAX_CB_BYTES:
            nav.append(
                InlineKeyboardButton(
                    "Next ➡️", callback_data=cb
                )
            )
    if nav:
        kb.append(nav)
    return kb


# ─────────────────────────────────────────────
# Download → PDF → Send
# ─────────────────────────────────────────────

async def _download_pdf(
    query,
    context,
    chapter_id: str,
    source: str,
    manga_title: str,
    chapter_num: str,
):
    chat_id = query.message.chat_id
    safe_title = esc(manga_title)
    safe_num = esc(str(chapter_num))

    # status card
    status = await context.bot.send_message(
        chat_id,
        (
            f"{'━' * 24}\n"
            f"📥  <b>Downloading</b>\n"
            f"{'━' * 24}\n\n"
            f"📖  {safe_title}\n"
            f"📑  Chapter {safe_num}\n\n"
            f"<code>⏳  Fetching page list …</code>"
        ),
        parse_mode="HTML",
    )

    urls = await fetcher.get_chapter_images(
        chapter_id, source
    )
    if not urls:
        await status.edit_text(
            "❌  <b>Failed</b> — images are "
            "unavailable or paywalled.",
            parse_mode="HTML",
        )
        return

    total = len(urls)
    await status.edit_text(
        (
            f"{'━' * 24}\n"
            f"📥  <b>Downloading</b>\n"
            f"{'━' * 24}\n\n"
            f"📖  {safe_title}\n"
            f"📑  Chapter {safe_num}\n\n"
            f"<code>⬇️  Downloading {total} pages …</code>"
        ),
        parse_mode="HTML",
    )

    session = await fetcher.get_session()

    async def _fetch(idx: int, url: str):
        async with DL_SEMAPHORE:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return idx, await resp.read()
            except Exception:
                pass
        return idx, None

    results = await asyncio.gather(
        *[_fetch(i, u) for i, u in enumerate(urls)]
    )

    # sort, collect, enforce size cap
    sorted_res = sorted(results)
    pages: list[bytes] = []
    cumulative = 0
    for _, raw in sorted_res:
        if raw is None:
            continue
        cumulative += len(raw)
        if cumulative > MAX_PDF_MB * 1024 * 1024:
            break
        pages.append(raw)

    await status.edit_text(
        (
            f"{'━' * 24}\n"
            f"📥  <b>Downloading</b>\n"
            f"{'━' * 24}\n\n"
            f"📖  {safe_title}\n"
            f"📑  Chapter {safe_num}\n\n"
            f"<code>📄  Building PDF "
            f"({len(pages)} pages) …</code>"
        ),
        parse_mode="HTML",
    )

    filename = (
        f"{sanitize_fn(manga_title)} "
        f"- Chapter {chapter_num}.pdf"
    )
    pdf = images_to_pdf(pages, filename)

    if pdf is None:
        await status.edit_text(
            "❌  Could not create PDF. "
            "All images may have failed to download.",
            parse_mode="HTML",
        )
        return

    await status.edit_text(
        (
            f"{'━' * 24}\n"
            f"📥  <b>Downloading</b>\n"
            f"{'━' * 24}\n\n"
            f"📖  {safe_title}\n"
            f"📑  Chapter {safe_num}\n\n"
            f"<code>📤  Uploading PDF …</code>"
        ),
        parse_mode="HTML",
    )

    await context.bot.send_document(
        chat_id=chat_id,
        document=pdf,
        caption=(
            f"📖  <b>{safe_title}</b>\n"
            f"📑  Chapter {safe_num}  ·  "
            f"{len(pages)} pages\n\n"
            f"<i>@{BOT_USERNAME}</i>"
        ),
        parse_mode="HTML",
    )
    await status.delete()


# ─────────────────────────────────────────────
# Main — Webhook (Render) or Polling (local)
# ─────────────────────────────────────────────

async def main():
    TOKEN = os.environ.get("TELEGRAM_TOKEN")
    if not TOKEN:
        logger.error("❌ Set TELEGRAM_TOKEN env var.")
        return

    PORT = int(os.environ.get("PORT", "8443"))
    RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL")

    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND, handle_text
        )
    )
    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    # graceful shutdown
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    # web routes
    async def index_handler(request):
        return web.Response(
            text=HTML_PAGE, content_type="text/html"
        )

    async def webhook_handler(request):
        try:
            data = await request.json()
            update = Update.de_json(
                data, application.bot
            )
            await application.update_queue.put(update)
            return web.Response()
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            return web.Response(status=500)

    await application.initialize()
    await application.start()

    if RENDER_URL:
        wh = f"{RENDER_URL}/{TOKEN}"
        await application.bot.set_webhook(url=wh)
        logger.info(f"Webhook → {wh}")

        web_app = web.Application()
        web_app.router.add_get("/", index_handler)
        web_app.router.add_post(
            f"/{TOKEN}", webhook_handler
        )
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logger.info(f"🚀 Listening on :{PORT}")

        await stop_event.wait()
        await site.stop()
        await runner.cleanup()
    else:
        logger.info("🔄 Polling mode (local)")
        await application.updater.start_polling()
        await stop_event.wait()
        await application.updater.stop()

    await fetcher.close()
    await application.stop()
    await application.shutdown()
    logger.info("👋 Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
