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
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
MAX_CB = 64
MAX_PDF_MB = 50
SEM = asyncio.Semaphore(6)
TIMEOUT = aiohttp.ClientTimeout(total=15)
UA = {"User-Agent": "OmegaMangaBot/2.1"}
BOT_USER = os.environ.get("BOT_USERNAME", "OmegaMangaBot")

# ─────────────────────────────────────────────
# Capped dictionary  (no memory leak)
# ─────────────────────────────────────────────
class Capped(OrderedDict):
    def __init__(self, cap=10_000):
        super().__init__()
        self._cap = cap

    def __setitem__(self, k, v):
        if k not in self and len(self) >= self._cap:
            self.popitem(last=False)
        super().__setitem__(k, v)


_ids = Capped(20_000)
_manga = Capped(5_000)
_chaps = Capped(20_000)


def short(orig: str) -> str:
    if len(orig) <= 12:
        return orig
    s = hashlib.md5(orig.encode()).hexdigest()[:12]
    _ids[s] = orig
    return s


def full(s: str) -> str:
    return _ids.get(s, s)


def safe_fn(n: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", n).strip()[:80]


def cb_ok(data: str) -> bool:
    return len(data.encode()) <= MAX_CB


# ─────────────────────────────────────────────
# Title + cover extractors  (fixes "Unknown Title")
# ─────────────────────────────────────────────
def extract_title(item: dict) -> str:
    attrs = item.get("attributes", {})
    titles = attrs.get("title", {})
    alts = attrs.get("altTitles", [])

    for lang in ("en", "ja-ro", "ko-ro", "zh-ro"):
        if titles.get(lang):
            return titles[lang]

    for alt in alts:
        if isinstance(alt, dict) and alt.get("en"):
            return alt["en"]

    for alt in alts:
        if isinstance(alt, dict):
            for lang in ("ja-ro", "ko-ro", "zh-ro"):
                if alt.get(lang):
                    return alt[lang]

    if titles:
        return next(iter(titles.values()))
    for alt in alts:
        if isinstance(alt, dict) and alt:
            return next(iter(alt.values()))

    return "Unknown Title"


def extract_cover(item: dict) -> str | None:
    for rel in item.get("relationships", []):
        if rel.get("type") == "cover_art":
            fn = rel.get("attributes", {}).get("fileName")
            if fn:
                return (
                    f"https://uploads.mangadex.org"
                    f"/covers/{item['id']}/{fn}.512.jpg"
                )
    return None


# ─────────────────────────────────────────────
# PDF builder
# ─────────────────────────────────────────────
def build_pdf(pages: list[bytes], name: str) -> io.BytesIO | None:
    imgs = []
    for raw in pages:
        if not raw:
            continue
        try:
            im = Image.open(io.BytesIO(raw))
            if im.mode != "RGB":
                im = im.convert("RGB")
            imgs.append(im)
        except Exception:
            continue
    if not imgs:
        return None
    buf = io.BytesIO()
    if len(imgs) == 1:
        imgs[0].save(buf, "PDF")
    else:
        imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:])
    buf.seek(0)
    buf.name = name
    return buf


# ─────────────────────────────────────────────
# Landing page
# ─────────────────────────────────────────────
HTML_PAGE = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Omega Manga Bot</title>
<style>
body{{background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.c{{background:#1e293b;padding:48px;border-radius:16px;text-align:center;
box-shadow:0 20px 40px rgba(0,0,0,.5);max-width:420px}}
h1{{color:#38bdf8;margin:0 0 8px}}
.s{{color:#4ade80;font-weight:700;font-size:1.1em;margin:16px 0}}
p{{color:#94a3b8;line-height:1.6}}
a{{display:inline-block;background:#38bdf8;color:#0f172a;padding:12px 28px;
text-decoration:none;border-radius:8px;font-weight:700;margin-top:12px}}
</style></head><body><div class="c">
<h1>🌀 Omega Manga Bot</h1>
<div class="s">🟢 Online</div>
<p>Search &amp; download manga chapters as PDF.</p>
<a href="https://t.me/{BOT_USER}">Open in Telegram</a>
</div></body></html>"""


# ─────────────────────────────────────────────
# Fetcher
# ─────────────────────────────────────────────
class MangaFetcher:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None

    async def _s(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=TIMEOUT, headers=UA)
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    @staticmethod
    def _norm(src: str) -> str:
        return "mdex" if src == "consumet" else src

    # ── TOP ───────────────────────────────────
    async def get_top_manga(self) -> list[dict]:
        s = await self._s()
        try:
            url = (
                "https://api.mangadex.org/manga"
                "?includes[]=cover_art"
                "&order[followedCount]=desc&limit=5"
                "&contentRating[]=safe&contentRating[]=suggestive"
                "&availableTranslatedLanguage[]=en"
            )
            async with s.get(url) as r:
                if r.status != 200:
                    return []
                data = await r.json()
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
            logger.error(f"Top: {e}")
        return []

    # ── SEARCH ────────────────────────────────
    async def search_manga(self, query: str) -> list[dict]:
        s = await self._s()
        enc = urllib.parse.quote(query)

        try:
            url = (
                f"https://api.mangadex.org/manga?title={enc}&limit=5"
                f"&order[relevance]=desc&includes[]=cover_art"
                f"&availableTranslatedLanguage[]=en"
            )
            async with s.get(url) as r:
                if r.status == 200:
                    data = await r.json()
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
            logger.warning(f"MDex search: {e}")

        try:
            url = f"https://api.comick.fun/v1.0/search?q={enc}&limit=5"
            async with s.get(url) as r:
                if r.status == 200:
                    data = await r.json()
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

        try:
            url = f"https://api.consumet.org/manga/mangadex/{enc}"
            async with s.get(url) as r:
                if r.status == 200:
                    data = await r.json()
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
    async def get_details(self, mid: str, src: str) -> dict:
        src = self._norm(src)
        s = await self._s()
        out = {"title": "Unknown", "desc": "No description.", "cover": None}
        try:
            if src == "mdex":
                url = f"https://api.mangadex.org/manga/{mid}?includes[]=cover_art"
                async with s.get(url) as r:
                    if r.status == 200:
                        item = (await r.json())["data"]
                        out["title"] = extract_title(item)
                        out["desc"] = item["attributes"]["description"].get(
                            "en", "No description."
                        )
                        out["cover"] = extract_cover(item)
            elif src == "comick":
                url = f"https://api.comick.fun/comic/{mid}"
                async with s.get(url) as r:
                    if r.status == 200:
                        c = (await r.json()).get("comic", {})
                        out["title"] = c.get("title", "Unknown")
                        out["desc"] = c.get("desc", "No description.")
                        covers = c.get("md_covers", [])
                        if covers:
                            b2 = covers[0].get("b2key", "")
                            if b2:
                                out["cover"] = f"https://meo.comick.pictures/{b2}"
        except Exception as e:
            logger.error(f"Details: {e}")
        return out

    # ── CHAPTERS ──────────────────────────────
    async def get_chapters(
        self, mid: str, src: str, offset: int = 0
    ) -> tuple[list[dict], bool]:
        src = self._norm(src)
        s = await self._s()
        limit = 10
        try:
            if src == "mdex":
                url = (
                    f"https://api.mangadex.org/manga/{mid}/feed"
                    f"?translatedLanguage[]=en&order[chapter]=desc"
                    f"&limit={limit}&offset={offset}"
                    f"&contentRating[]=safe&contentRating[]=suggestive"
                    f"&contentRating[]=erotica"
                )
                async with s.get(url) as r:
                    if r.status != 200:
                        logger.warning(f"Chapters API status {r.status}")
                        return [], False
                    data = await r.json()
                    chs = [
                        {"id": c["id"], "num": c["attributes"].get("chapter") or "?"}
                        for c in data.get("data", [])
                    ]
                    total = data.get("total", 0)
                    logger.info(f"Chapters: got {len(chs)}, total={total}")
                    return chs, total > offset + limit

            elif src == "comick":
                page = offset // limit + 1
                url = (
                    f"https://api.comick.fun/comic/{mid}/chapters"
                    f"?lang=en&limit={limit}&page={page}"
                )
                async with s.get(url) as r:
                    if r.status != 200:
                        return [], False
                    data = await r.json()
                    raw = data.get("chapters", [])
                    chs = [
                        {"id": c["hid"], "num": c.get("chap") or "?"}
                        for c in raw
                    ]
                    return chs, len(raw) == limit
        except Exception as e:
            logger.error(f"Chapters: {e}")
        return [], False

    # ── IMAGES ────────────────────────────────
    async def get_images(self, cid: str, src: str) -> list[str]:
        src = self._norm(src)
        s = await self._s()
        try:
            if src == "mdex":
                url = f"https://api.mangadex.org/at-home/server/{cid}"
                async with s.get(url) as r:
                    if r.status != 200:
                        return []
                    data = await r.json()
                    base = data["baseUrl"]
                    h = data["chapter"]["hash"]
                    return [f"{base}/data/{h}/{i}" for i in data["chapter"]["data"]]
            elif src == "comick":
                url = f"https://api.comick.fun/chapter/{cid}"
                async with s.get(url) as r:
                    if r.status != 200:
                        return []
                    data = await r.json()
                    return [
                        i["url"]
                        for i in data.get("chapter", {}).get("images", [])
                    ]
        except Exception as e:
            logger.error(f"Images: {e}")
        return []


fetcher = MangaFetcher()


# ─────────────────────────────────────────────
# Keyboard builders
# ─────────────────────────────────────────────

def _manga_kb(items: list[dict]) -> list[list[InlineKeyboardButton]]:
    """Build manga selection keyboard (one per row)."""
    kb = []
    for i, m in enumerate(items, 1):
        sid = short(m["id"])
        _manga[sid] = {"title": m["title"], "source": m["source"], "cover": m.get("cover")}
        cb = f"m|{m['source']}|{sid}"
        if cb_ok(cb):
            kb.append([InlineKeyboardButton(f"{i}. {m['title']}", callback_data=cb)])
    return kb


def _chapter_kb(
    chapters: list[dict],
    source: str,
    manga_id: str,
    manga_title: str,
    offset: int,
    has_next: bool,
) -> list[list[InlineKeyboardButton]]:
    """Build chapter grid (2 per row) + nav."""
    msid = short(manga_id)
    kb: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for ch in chapters:
        csid = short(ch["id"])
        _chaps[csid] = {"num": ch["num"], "title": manga_title}
        cb = f"d|{source}|{csid}"
        if not cb_ok(cb):
            continue
        row.append(InlineKeyboardButton(f"📄 Ch. {ch['num']}", callback_data=cb))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)

    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        cb = f"p|{source}|{msid}|{max(0, offset - 10)}"
        if cb_ok(cb):
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=cb))
    if has_next:
        cb = f"p|{source}|{msid}|{offset + 10}"
        if cb_ok(cb):
            nav.append(InlineKeyboardButton("Next ➡️", callback_data=cb))
    if nav:
        kb.append(nav)
    return kb


# ─────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Loading…")
    top = await fetcher.get_top_manga()

    text = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "   🌀 <b>OMEGA MANGA BOT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send any title to search &amp; download\n"
        "chapters as <b>PDF</b> instantly.\n\n"
    )

    if top:
        text += "🔥 <b>Trending Now</b>\n─────────────────────"
        kb = _manga_kb(top)
    else:
        text += "<i>Could not load trending. Just type a title to search!</i>"
        kb = []

    await msg.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────────
# /help
# ─────────────────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "   ❓ <b>HOW TO USE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "1️⃣  Type any manga / manhwa name\n"
        "2️⃣  Pick a result\n"
        "3️⃣  Tap a chapter button\n"
        "4️⃣  Get it as <b>PDF</b>\n\n"
        "📎 File format: <code>Title - Chapter X.pdf</code>",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────
# Text search
# ─────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.message.text.strip()
    if not q:
        return

    msg = await update.message.reply_text(
        f"🔍 Searching <b>{esc(q)}</b> …", parse_mode="HTML"
    )

    results = await fetcher.search_manga(q)

    if not results:
        await msg.edit_text(
            "❌ <b>No results.</b>\n<i>Try a different title or spelling.</i>",
            parse_mode="HTML",
        )
        return

    kb = _manga_kb(results)
    await msg.edit_text(
        f"✅ <b>Results for</b> \"{esc(q)}\"\n─────────────────────\nTap a title:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ─────────────────────────────────────────────
# Callback router
# ─────────────────────────────────────────────

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("|")
    action = parts[0]

    try:
        # ── MANGA SELECTED ────────────────────
        if action == "m" and len(parts) == 3:
            await _show_manga(q, context, parts[1], parts[2])

        # ── CHAPTER PAGE ──────────────────────
        elif action == "p" and len(parts) == 4:
            await _paginate(q, context, parts[1], parts[2], int(parts[3]))

        # ── DOWNLOAD ──────────────────────────
        elif action == "d" and len(parts) == 3:
            await _download(q, context, parts[1], parts[2])

    except Exception as e:
        logger.error(f"Callback error: {e}", exc_info=True)
        try:
            await q.message.reply_text("⚠️ Something went wrong. Please try again.")
        except Exception:
            pass


# ─────────────────────────────────────────────
# Show manga  (TWO separate messages = fix)
# ─────────────────────────────────────────────

async def _show_manga(q, context, source: str, sid: str):
    manga_id = full(sid)
    chat_id = q.message.chat_id
    cached = _manga.get(sid, {})

    # ── loading state ──
    await q.edit_message_text("⏳ Loading details & chapters …")

    # ── fetch both concurrently ──
    details, (chapters, has_next) = await asyncio.gather(
        fetcher.get_details(manga_id, source),
        fetcher.get_chapters(manga_id, source, 0),
    )

    title = details.get("title") or cached.get("title", "Unknown")
    desc = details.get("desc", "No description.")
    cover = details.get("cover") or cached.get("cover")

    # update cache
    _manga[sid] = {"title": title, "source": source, "cover": cover}

    # ── delete loading message ──
    try:
        await q.message.delete()
    except Exception:
        pass

    # ── MESSAGE 1 : Cover + info ──
    #    (photo caption ≤ 1024 chars, so trim desc)
    short_desc = desc[:600] + "…" if len(desc) > 600 else desc
    info_caption = (
        f"📖  <b>{esc(title)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{esc(short_desc)}"
    )

    if cover:
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=cover,
                caption=info_caption,
                parse_mode="HTML",
            )
        except Exception:
            await context.bot.send_message(
                chat_id=chat_id,
                text=info_caption,
                parse_mode="HTML",
            )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=info_caption,
            parse_mode="HTML",
        )

    # ── MESSAGE 2 : Chapter buttons (always separate) ──
    if not chapters:
        retry_cb = f"m|{source}|{sid}"
        retry_kb = []
        if cb_ok(retry_cb):
            retry_kb = [[InlineKeyboardButton("🔄  Retry", callback_data=retry_cb)]]
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"📚  <b>{esc(title)}</b>\n"
                f"─────────────────────\n\n"
                f"⚠️  No chapters found.\n\n"
                f"<i>The source may be rate-limiting or\n"
                f"this title has no English chapters yet.</i>"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(retry_kb) if retry_kb else None,
        )
        return

    kb = _chapter_kb(chapters, source, manga_id, title, 0, has_next)

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"📚  <b>{esc(title)}</b> — Chapters\n"
            f"─────────────────────\n"
            f"Tap a chapter to download as PDF:"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ─────────────────────────────────────────────
# Pagination  (edits the chapter-list message)
# ─────────────────────────────────────────────

async def _paginate(q, context, source: str, sid: str, offset: int):
    manga_id = full(sid)
    meta = _manga.get(sid, {})
    title = meta.get("title", "Unknown")

    chapters, has_next = await fetcher.get_chapters(manga_id, source, offset)

    if not chapters:
        await q.answer("No more chapters found.", show_alert=True)
        return

    kb = _chapter_kb(chapters, source, manga_id, title, offset, has_next)

    await q.edit_message_text(
        text=(
            f"📚  <b>{esc(title)}</b> — Chapters\n"
            f"─────────────────────\n"
            f"Tap a chapter to download as PDF:"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ─────────────────────────────────────────────
# Download chapter → PDF → Send
# ─────────────────────────────────────────────

async def _download(q, context, source: str, csid: str):
    chapter_id = full(csid)
    meta = _chaps.get(csid, {})
    manga_title = meta.get("title", "Manga")
    chapter_num = meta.get("num", "0")
    chat_id = q.message.chat_id

    safe_t = esc(manga_title)
    safe_n = esc(str(chapter_num))

    def _status(step1="⏳", step2="⬜", step3="⬜", step4="⬜", extra=""):
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📥  <b>Downloading</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📖  {safe_t}\n"
            f"📑  Chapter {safe_n}\n\n"
            f"{step1}  Fetch page list\n"
            f"{step2}  Download images\n"
            f"{step3}  Build PDF\n"
            f"{step4}  Upload to Telegram"
            f"{extra}"
        )

    status = await context.bot.send_message(
        chat_id=chat_id,
        text=_status("⏳"),
        parse_mode="HTML",
    )

    urls = await fetcher.get_images(chapter_id, source)
    if not urls:
        await status.edit_text(
            _status("❌") +
            "\n\n<i>Images unavailable or paywalled.</i>",
            parse_mode="HTML",
        )
        return

    await status.edit_text(
        _status("✅", f"⏳  ({len(urls)} pages)"),
        parse_mode="HTML",
    )

    session = await fetcher._s()

    async def _get(idx, url):
        async with SEM:
            try:
                async with session.get(url) as r:
                    if r.status == 200:
                        return idx, await r.read()
            except Exception:
                pass
        return idx, None

    results = await asyncio.gather(*[_get(i, u) for i, u in enumerate(urls)])

    # sort + cap size
    ordered = sorted(results)
    pages, total_bytes = [], 0
    for _, raw in ordered:
        if raw is None:
            continue
        total_bytes += len(raw)
        if total_bytes > MAX_PDF_MB * 1024 * 1024:
            break
        pages.append(raw)

    if not pages:
        await status.edit_text(
            _status("✅", "❌") +
            "\n\n<i>All image downloads failed.</i>",
            parse_mode="HTML",
        )
        return

    await status.edit_text(
        _status("✅", f"✅  ({len(pages)} pages)", "⏳"),
        parse_mode="HTML",
    )

    filename = f"{safe_fn(manga_title)} - Chapter {chapter_num}.pdf"
    pdf = build_pdf(pages, filename)

    if pdf is None:
        await status.edit_text(
            _status("✅", "✅", "❌") +
            "\n\n<i>Failed to build PDF.</i>",
            parse_mode="HTML",
        )
        return

    await status.edit_text(
        _status("✅", "✅", "✅", "⏳"),
        parse_mode="HTML",
    )

    await context.bot.send_document(
        chat_id=chat_id,
        document=pdf,
        caption=(
            f"📖  <b>{safe_t}</b>\n"
            f"📑  Chapter {safe_n}  ·  {len(pages)} pages\n\n"
            f"<i>@{BOT_USER}</i>"
        ),
        parse_mode="HTML",
    )
    await status.delete()


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

async def main():
    TOKEN = os.environ.get("TELEGRAM_TOKEN")
    if not TOKEN:
        logger.error("Set TELEGRAM_TOKEN env var.")
        return

    PORT = int(os.environ.get("PORT", "8443"))
    RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(on_button))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def index(request):
        return web.Response(text=HTML_PAGE, content_type="text/html")

    async def webhook(request):
        try:
            data = await request.json()
            update = Update.de_json(data, app.bot)
            await app.update_queue.put(update)
            return web.Response()
        except Exception as e:
            logger.error(f"Webhook: {e}")
            return web.Response(status=500)

    await app.initialize()
    await app.start()

    if RENDER_URL:
        wh = f"{RENDER_URL}/{TOKEN}"
        await app.bot.set_webhook(url=wh)
        logger.info(f"Webhook → {wh}")

        wa = web.Application()
        wa.router.add_get("/", index)
        wa.router.add_post(f"/{TOKEN}", webhook)
        runner = web.AppRunner(wa)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logger.info(f"🚀 :{PORT}")

        await stop.wait()
        await site.stop()
        await runner.cleanup()
    else:
        logger.info("Polling mode")
        await app.updater.start_polling()
        await stop.wait()
        await app.updater.stop()

    await fetcher.close()
    await app.stop()
    await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
