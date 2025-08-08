import os
import json
import asyncio
import threading
from datetime import datetime, timezone, time
import math
from typing import Any, Dict, List, Optional, Tuple
import re
from urllib.parse import quote_plus

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from telegram import (
    Update,
    InputMediaPhoto,
    InputMediaVideo,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

SUBSCRIBERS_FILE = "subscribers.json"

# Simple in-memory cache: { (locale, country, kind): {"at": datetime, "items": list} }
FREE_GAMES_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_SECONDS = 600


def load_json(path: str, default: Any) -> Any:
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def ensure_files_exist() -> None:
    if not os.path.exists(SUBSCRIBERS_FILE):
        save_json(SUBSCRIBERS_FILE, {"chat_ids": [], "users": {}, "offer_subs": {}})


# --- Minimal HTTP server for Koyeb health checks ---
def _create_health_app() -> web.Application:
    app = web.Application()

    async def root_handler(request: web.Request) -> web.Response:
        return web.Response(text="OK", content_type="text/plain")

    async def health_handler(request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "time": datetime.now(timezone.utc).isoformat(),
        })

    app.router.add_get("/", root_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/healthz", health_handler)
    return app


async def _run_http_server_forever() -> None:
    port = int(os.getenv("PORT", "8000"))
    app = _create_health_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Health server listening on 0.0.0.0:{port}")
    # Keep running forever
    while True:
        await asyncio.sleep(3600)


def start_health_server_in_background() -> None:
    thread = threading.Thread(target=lambda: asyncio.run(_run_http_server_forever()), daemon=True)
    thread.start()


def _cache_key(locale: str, country: str, kind: str) -> str:
    return f"{locale}|{country}|{kind}"


def _get_cached(locale: str, country: str, kind: str) -> Optional[List[Dict[str, Any]]]:
    key = _cache_key(locale, country, kind)
    entry = FREE_GAMES_CACHE.get(key)
    if not entry:
        return None
    if (datetime.now(timezone.utc) - entry["at"]).total_seconds() > CACHE_TTL_SECONDS:
        FREE_GAMES_CACHE.pop(key, None)
        return None
    return entry["items"]


def _set_cached(locale: str, country: str, kind: str, items: List[Dict[str, Any]]) -> None:
    key = _cache_key(locale, country, kind)
    FREE_GAMES_CACHE[key] = {"at": datetime.now(timezone.utc), "items": items}


async def fetch_free_games(locale: str = "en-US", country: str = "US") -> List[Dict[str, Any]]:
    cached = _get_cached(locale, country, kind="current")
    if cached is not None:
        return cached
    url = (
        "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
        f"?locale={locale}&country={country}&allowCountries={country}"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=20) as resp:
            resp.raise_for_status()
            data = await resp.json()

    elements = (
        data.get("data", {})
        .get("Catalog", {})
        .get("searchStore", {})
        .get("elements", [])
    )

    now = datetime.now(timezone.utc)
    free_now: List[Dict[str, Any]] = []
    for el in elements:
        promotions = el.get("promotions") or {}
        current = promotions.get("promotionalOffers") or []
        if not current:
            continue
        # Check if any current promotional offer window is active now
        is_active = False
        for offer in current:
            for po in offer.get("promotionalOffers", []):
                start_str = po.get("startDate")
                end_str = po.get("endDate")
                try:
                    start = datetime.fromisoformat(start_str.replace("Z", "+00:00")) if start_str else None
                    end = datetime.fromisoformat(end_str.replace("Z", "+00:00")) if end_str else None
                except Exception:
                    start = end = None
                if (start is None or start <= now) and (end is None or now < end):
                    is_active = True
                    break
            if is_active:
                break
        if not is_active:
            continue
        free_now.append(el)

    _set_cached(locale, country, kind="current", items=free_now)
    return free_now


async def fetch_upcoming_games(locale: str = "en-US", country: str = "US") -> List[Dict[str, Any]]:
    cached = _get_cached(locale, country, kind="upcoming")
    if cached is not None:
        return cached
    url = (
        "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
        f"?locale={locale}&country={country}&allowCountries={country}"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=20) as resp:
            resp.raise_for_status()
            data = await resp.json()

    elements = (
        data.get("data", {})
        .get("Catalog", {})
        .get("searchStore", {})
        .get("elements", [])
    )

    now = datetime.now(timezone.utc)
    upcoming: List[Dict[str, Any]] = []
    for el in elements:
        promotions = el.get("promotions") or {}
        upcoming_offers = promotions.get("upcomingPromotionalOffers") or []
        if not upcoming_offers:
            continue
        # Pick if any upcoming window is in the future
        is_future = False
        starts_at: Optional[datetime] = None
        for offer in upcoming_offers:
            for po in offer.get("promotionalOffers", []):
                start_str = po.get("startDate")
                try:
                    start = datetime.fromisoformat(start_str.replace("Z", "+00:00")) if start_str else None
                except Exception:
                    start = None
                if start and start > now:
                    is_future = True
                    if not starts_at or start < starts_at:
                        starts_at = start
        if is_future:
            el["__upcomingStart"] = starts_at.isoformat() if starts_at else None
            upcoming.append(el)

    # Sort by soonest start time
    upcoming.sort(key=lambda e: e.get("__upcomingStart") or "")
    _set_cached(locale, country, kind="upcoming", items=upcoming)
    return upcoming


def pick_image_url(el: Dict[str, Any]) -> Optional[str]:
    key_images = el.get("keyImages") or []
    preferred_types = [
        "OfferImageWide",
        "DieselStoreFrontWide",
        "DieselStoreFront",
        "Thumbnail",
    ]
    # First pass: preferred types
    for ptype in preferred_types:
        for img in key_images:
            if img.get("type") == ptype and img.get("url"):
                return img.get("url")
    # Fallback: any image
    if key_images:
        return key_images[0].get("url")
    return None


def build_store_url(el: Dict[str, Any], locale: str = "en-US") -> str:
    def slug_to_url(slug: str) -> str:
        slug = (slug or "").lstrip("/")
        if slug.startswith(("p/", "bundles/", "store/")):
            return f"https://store.epicgames.com/{locale}/{slug}"
        return f"https://store.epicgames.com/{locale}/p/{slug}"

    # Prefer catalogNs.mappings with pageType=productHome
    mappings = (el.get("catalogNs") or {}).get("mappings") or []
    for m in mappings:
        if (m.get("pageType") or "").lower() == "producthome" and m.get("pageSlug"):
            return slug_to_url(m.get("pageSlug"))

    # Next: any mapping with pageSlug
    for m in mappings:
        if m.get("pageSlug"):
            return slug_to_url(m.get("pageSlug"))

    # Next: offerMappings if present
    for m in (el.get("offerMappings") or []):
        if m.get("pageSlug"):
            return slug_to_url(m.get("pageSlug"))

    # Next: productSlug (may already include path segments)
    product_slug = el.get("productSlug")
    if product_slug:
        return slug_to_url(product_slug)

    # Fallback: urlSlug as last resort
    url_slug = el.get("urlSlug")
    if url_slug:
        return slug_to_url(url_slug)

    return "https://store.epicgames.com/"


def get_user_prefs(chat_id: int) -> Dict[str, str]:
    ensure_files_exist()
    data = load_json(SUBSCRIBERS_FILE, {"chat_ids": [], "users": {}, "offer_subs": {}})
    users = data.get("users", {})
    prefs = users.get(str(chat_id)) or {}
    return {
        "locale": prefs.get("locale", "en-US"),
        "country": prefs.get("country", "US"),
    }


def set_user_pref(chat_id: int, key: str, value: str) -> None:
    ensure_files_exist()
    data = load_json(SUBSCRIBERS_FILE, {"chat_ids": [], "users": {}, "offer_subs": {}})
    users = data.setdefault("users", {})
    user = users.setdefault(str(chat_id), {})
    user[key] = value
    save_json(SUBSCRIBERS_FILE, data)


def get_offer_id(el: Dict[str, Any]) -> Optional[str]:
    offer_id = el.get("id")
    if offer_id:
        return str(offer_id)
    items = el.get("items") or []
    if items and isinstance(items, list) and isinstance(items[0], dict):
        iid = items[0].get("id")
        if iid:
            return str(iid)
    return None


def is_subscribed_to_offer(chat_id: int, offer_id: str) -> bool:
    ensure_files_exist()
    data = load_json(SUBSCRIBERS_FILE, {"chat_ids": [], "users": {}, "offer_subs": {}})
    subs = data.get("offer_subs", {}).get(str(chat_id), {})
    return offer_id in subs


def subscribe_to_offer(chat_id: int, offer_id: str, title: str, page_slug: Optional[str]) -> None:
    ensure_files_exist()
    data = load_json(SUBSCRIBERS_FILE, {"chat_ids": [], "users": {}, "offer_subs": {}})
    user_subs = data.setdefault("offer_subs", {}).setdefault(str(chat_id), {})
    user_subs[offer_id] = {
        "title": title,
        "pageSlug": page_slug or "",
        "notified": False,
    }
    save_json(SUBSCRIBERS_FILE, data)


def unsubscribe_from_offer(chat_id: int, offer_id: str) -> None:
    ensure_files_exist()
    data = load_json(SUBSCRIBERS_FILE, {"chat_ids": [], "users": {}, "offer_subs": {}})
    user_subs = data.setdefault("offer_subs", {}).setdefault(str(chat_id), {})
    if offer_id in user_subs:
        user_subs.pop(offer_id, None)
        save_json(SUBSCRIBERS_FILE, data)


async def send_subscriptions_list(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_files_exist()
    data = load_json(SUBSCRIBERS_FILE, {"chat_ids": [], "users": {}, "offer_subs": {}})
    user_subs: Dict[str, Dict[str, Any]] = data.get("offer_subs", {}).get(str(chat_id), {})
    if not user_subs:
        await context.bot.send_message(chat_id=chat_id, text="You have no game notifications set.")
        return
    rows = []
    for offer_id, meta in list(user_subs.items())[:12]:  # limit rows
        title = meta.get("title") or offer_id
        page_slug = meta.get("pageSlug") or ""
        url = f"https://store.epicgames.com/en-US/p/{page_slug}" if page_slug else "https://store.epicgames.com/"
        rows.append([
            InlineKeyboardButton(text=title[:48], url=url),
            InlineKeyboardButton(text="Unsubscribe", callback_data=f"offer_unsub:{offer_id}"),
        ])
    markup = InlineKeyboardMarkup(rows)
    await context.bot.send_message(chat_id=chat_id, text="Your game notifications:", reply_markup=markup)


async def send_free_games(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    prefs = get_user_prefs(chat_id)
    games = await fetch_free_games(locale=prefs["locale"], country=prefs["country"])
    if not games:
        await context.bot.send_message(chat_id=chat_id, text="No free games right now.")
        return

    for el in games:
        title = el.get("title", "Free Game")
        url = build_store_url(el)
        image_url = pick_image_url(el)
        caption = f"<b>{title}</b>\n<a href=\"{url}\">Claim on Epic Games Store</a>"

        # Try to fetch a trailer video using the product page slug
        page_slug = get_page_slug(el)
        namespace = str(el.get("namespace") or (el.get("catalogNs") or {}).get("namespace") or "").strip()
        trailer_video_url: Optional[str] = None
        if page_slug:
            try:
                trailer_video_url, _ = await fetch_trailer_urls(page_slug, namespace=namespace)
            except Exception:
                trailer_video_url = None

        # Prefer sending photo + trailer as a media group if both exist
        if image_url and trailer_video_url:
            media = [
                InputMediaPhoto(media=image_url, caption=caption, parse_mode=ParseMode.HTML),
                InputMediaVideo(media=trailer_video_url),
            ]
            try:
                await context.bot.send_media_group(chat_id=chat_id, media=media)
                continue
            except Exception:
                # Fallback to separate sends
                pass

        # If media group not possible, send photo (with caption) then trailer (if any)
        if image_url:
            try:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=image_url,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )

        if trailer_video_url:
            try:
                await context.bot.send_video(chat_id=chat_id, video=trailer_video_url)
            except Exception:
                pass


def get_page_slug(el: Dict[str, Any]) -> Optional[str]:
    def normalize_slug(slug: str) -> str:
        slug = (slug or "").strip().lstrip("/")
        # Remove leading path like p/, bundles/, store/
        parts = slug.split("/")
        if parts and parts[0] in {"p", "bundles", "store"}:
            parts = parts[1:]
        return "/".join(parts)

    mappings = (el.get("catalogNs") or {}).get("mappings") or []
    for m in mappings:
        if ((m.get("pageType") or "").lower() == "producthome") and m.get("pageSlug"):
            return normalize_slug(m.get("pageSlug"))
    for m in mappings:
        if m.get("pageSlug"):
            return normalize_slug(m.get("pageSlug"))

    for m in (el.get("offerMappings") or []):
        if m.get("pageSlug"):
            return normalize_slug(m.get("pageSlug"))

    product_slug = el.get("productSlug")
    if product_slug:
        return normalize_slug(product_slug)

    url_slug = el.get("urlSlug")
    if url_slug:
        return normalize_slug(url_slug)

    return None


async def fetch_trailer_urls(page_slug: str, locale: str = "en-US", namespace: str = "") -> Tuple[Optional[str], Optional[str]]:
    locales_to_try = [locale, "en", "en-GB"]
    slug_candidates = [page_slug]
    stripped = re.sub(r"-[0-9a-f]{6}$", "", page_slug, flags=re.IGNORECASE)
    if stripped and stripped != page_slug:
        slug_candidates.append(stripped)
    if namespace:
        slug_candidates += [
            f"{namespace}/{page_slug}",
            f"{namespace}/{stripped}" if stripped else None,
        ]
        slug_candidates = [s for s in slug_candidates if s]

    data: Optional[Dict[str, Any]] = None
    async with aiohttp.ClientSession() as session:
        for loc in locales_to_try:
            base = f"https://store-content.ak.epicgames.com/api/{loc}/content/products/"
            for attempt_slug in slug_candidates:
                content_url = base + attempt_slug
                try:
                    async with session.get(content_url, timeout=20) as resp:
                        resp.raise_for_status()
                        data = await resp.json()
                        if attempt_slug != page_slug or loc != locale:
                            print(f"Content fallback used: '{page_slug}' -> '{attempt_slug}' (locale {loc})")
                        break
                except Exception as exc:
                    print(f"Failed to fetch content for slug '{attempt_slug}' (locale {loc}): {exc}")
                    data = None
            if data is not None:
                break
        if data is None:
            return None, None

    # Prefer structured location: pages -> productHome -> modules
    pages: List[Dict[str, Any]] = data.get("pages") or []
    modules_candidates: List[Any] = []
    for page in pages:
        if (page.get("_type") or page.get("type") or "").lower() == "producthome":
            modules_candidates.append(page.get("modules"))
    # If not found, check top-level modules too
    if not modules_candidates and "modules" in data:
        modules_candidates.append(data.get("modules"))

    def pick_from_sources(sources: Any) -> Optional[str]:
        if not isinstance(sources, list):
            return None
        for src in sources:
            url = (src or {}).get("src") or (src or {}).get("url")
            if not isinstance(url, str):
                continue
            low = url.lower()
            if low.endswith( (".mp4", ".webm", ".mov") ) or ".mp4" in low:
                return url
        return None

    def scan_modules(obj: Any) -> Tuple[Optional[str], Optional[str]]:
        direct: Optional[str] = None
        yt: Optional[str] = None

        def consider(value: str) -> None:
            nonlocal direct, yt
            v = (value or "").strip()
            low = v.lower()
            if (low.endswith(".mp4") or low.endswith(".webm") or low.endswith(".mov") or ".mp4" in low) and not direct:
                direct = v
            if ("youtube.com" in low or "youtu.be" in low) and not yt:
                yt = v

        if isinstance(obj, dict):
            # Common patterns
            if obj.get("provider") == "youtube" and obj.get("id") and not yt:
                yt = f"https://youtu.be/{obj.get('id')}"
            if "youTubeUrl" in obj and obj["youTubeUrl"] and not yt:
                consider(obj["youTubeUrl"])
            if "youtubeUrl" in obj and obj["youtubeUrl"] and not yt:
                consider(obj["youtubeUrl"])
            if "videoUrl" in obj and obj["videoUrl"] and not direct:
                consider(obj["videoUrl"])
            if "video" in obj and isinstance(obj["video"], dict):
                # e.g., { video: { sources: [{src: ...}] } }
                cand = pick_from_sources(obj["video"].get("sources"))
                direct = direct or cand
            if "sources" in obj:
                cand = pick_from_sources(obj.get("sources"))
                direct = direct or cand
            # Recurse
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    d, y = scan_modules(v)
                    direct = direct or d
                    yt = yt or y
        elif isinstance(obj, list):
            for item in obj:
                d, y = scan_modules(item)
                direct = direct or d
                yt = yt or y
        elif isinstance(obj, str):
            consider(obj)
        return direct, yt

    direct_video: Optional[str] = None
    youtube_link: Optional[str] = None
    for mc in modules_candidates:
        d, y = scan_modules(mc)
        direct_video = direct_video or d
        youtube_link = youtube_link or y

    if not direct_video and not youtube_link:
        # Last resort: global scan
        d, y = scan_modules(data)
        direct_video = direct_video or d
        youtube_link = youtube_link or y

    if not direct_video and not youtube_link:
        print(f"No trailer found for slug '{page_slug}' from content API")
    else:
        print(f"Trailer for '{page_slug}': direct={bool(direct_video)} youtube={youtube_link}")

    return direct_video, youtube_link


async def search_youtube_trailer(query: str) -> Optional[str]:
    search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(search_url, timeout=20) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()
    m = re.search(r'\"videoId\":\"([A-Za-z0-9_-]{11})\"', html)
    if not m:
        return None
    return f"https://youtu.be/{m.group(1)}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Show Free Games", callback_data="action:free")],
        [InlineKeyboardButton("Show Upcoming", callback_data="action:upcoming")],
        [
            InlineKeyboardButton("Subscribe", callback_data="sub:1"),
            InlineKeyboardButton("Unsubscribe", callback_data="sub:0"),
        ],
    ])
    await update.message.reply_text(
        "Hi! Use /freegames to see this week's free Epic Games.\n"
        "Use /subscribe to get a daily reminder while the bot is running.\n"
        "You can also /setlocale and /setcountry.",
        reply_markup=keyboard,
    )


async def freegames_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_free_games(update.effective_chat.id, context)


async def upcoming_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    prefs = get_user_prefs(chat_id)
    games = await fetch_upcoming_games(locale=prefs["locale"], country=prefs["country"])
    if not games:
        await update.message.reply_text("No upcoming free games found.")
        return
    now = datetime.now(timezone.utc)
    for el in games[:6]:  # limit spam
        title = el.get("title", "Upcoming Free Game")
        url = build_store_url(el)
        image_url = pick_image_url(el)
        start_iso = el.get("__upcomingStart")
        when = "Coming soon"
        if start_iso:
            try:
                start_dt = datetime.fromisoformat(start_iso)
                # Compute days delta in whole days (ceil if partial day)
                delta_days = math.ceil((start_dt - now).total_seconds() / 86400)
                # Format as: in 3 days (08.14)
                when = f"in {delta_days} day{'s' if delta_days != 1 else ''} ({start_dt.strftime('%m.%d')})"
            except Exception:
                pass
        caption = f"<b>{title}</b>\n{when}\n<a href=\"{url}\">View on Epic</a>"
        # Show per-game notify toggle: Subscribe if not subscribed, Unsubscribe if subscribed
        offer_id = get_offer_id(el)
        keyboard = None
        if offer_id:
            if is_subscribed_to_offer(chat_id, offer_id):
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Unnotify This Game", callback_data=f"offer_unsub:{offer_id}")]
                ])
            else:
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Notify When Free", callback_data=f"offer_sub:{offer_id}")]
                ])
        if image_url:
            try:
                await context.bot.send_photo(chat_id=chat_id, photo=image_url, caption=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            except Exception:
                await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        else:
            await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def subscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_files_exist()
    data = load_json(SUBSCRIBERS_FILE, {"chat_ids": [], "users": {}})
    chat_ids: List[int] = data.get("chat_ids", [])
    chat_id = update.effective_chat.id
    if chat_id not in chat_ids:
        chat_ids.append(chat_id)
        data["chat_ids"] = chat_ids
        save_json(SUBSCRIBERS_FILE, data)
        await update.message.reply_text("Subscribed. You'll get a daily reminder.")
    else:
        await update.message.reply_text("You're already subscribed.")


async def unsubscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_files_exist()
    data = load_json(SUBSCRIBERS_FILE, {"chat_ids": [], "users": {}})
    chat_ids: List[int] = data.get("chat_ids", [])
    chat_id = update.effective_chat.id
    if chat_id in chat_ids:
        chat_ids.remove(chat_id)
        data["chat_ids"] = chat_ids
        save_json(SUBSCRIBERS_FILE, data)
        await update.message.reply_text("Unsubscribed.")
    else:
        await update.message.reply_text("You were not subscribed.")


async def setlocale_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /setlocale en-US")
        return
    locale = context.args[0]
    set_user_pref(update.effective_chat.id, "locale", locale)
    await update.message.reply_text(f"Locale set to {locale}")


async def setcountry_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /setcountry US")
        return
    country = context.args[0]
    set_user_pref(update.effective_chat.id, "country", country.upper())
    await update.message.reply_text(f"Country set to {country.upper()}")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("sub:"):
        flag = data.split(":", 1)[1]
        if flag == "1":
            # Reuse subscribe logic
            ensure_files_exist()
            store = load_json(SUBSCRIBERS_FILE, {"chat_ids": [], "users": {}})
            chat_ids: List[int] = store.get("chat_ids", [])
            if q.message.chat.id not in chat_ids:
                chat_ids.append(q.message.chat.id)
                store["chat_ids"] = chat_ids
                save_json(SUBSCRIBERS_FILE, store)
                await q.edit_message_reply_markup(reply_markup=None)
                await q.message.reply_text("Subscribed. You'll get a daily reminder.")
            else:
                await q.message.reply_text("You're already subscribed.")
        else:
            store = load_json(SUBSCRIBERS_FILE, {"chat_ids": [], "users": {}})
            chat_ids: List[int] = store.get("chat_ids", [])
            if q.message.chat.id in chat_ids:
                chat_ids.remove(q.message.chat.id)
                store["chat_ids"] = chat_ids
                save_json(SUBSCRIBERS_FILE, store)
                await q.edit_message_reply_markup(reply_markup=None)
                await q.message.reply_text("Unsubscribed.")
            else:
                await q.message.reply_text("You were not subscribed.")
    elif data == "action:free":
        await send_free_games(q.message.chat.id, context)
    elif data == "action:upcoming":
        # simulate a command
        class Dummy:
            effective_chat = q.message.chat
            message = q.message
        await upcoming_cmd(Dummy(), context)
    elif data.startswith("offer_sub:"):
        offer_id = data.split(":", 1)[1]
        # We need at least the title and page slug to store; fetch from a cached view by scanning recent free/upcoming
        chat_id = q.message.chat.id
        prefs = get_user_prefs(chat_id)
        # Merge current and upcoming for lookup
        current = await fetch_free_games(locale=prefs["locale"], country=prefs["country"])
        upcoming = await fetch_upcoming_games(locale=prefs["locale"], country=prefs["country"])
        el = next((e for e in current + upcoming if get_offer_id(e) == offer_id), None)
        title = el.get("title") if el else offer_id
        page_slug = get_page_slug(el) if el else None
        subscribe_to_offer(chat_id, offer_id, title, page_slug)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await q.message.reply_text(f"You'll be notified when '{title}' becomes free.")
    elif data.startswith("offer_unsub:"):
        offer_id = data.split(":", 1)[1]
        chat_id = q.message.chat.id
        unsubscribe_from_offer(chat_id, offer_id)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await q.message.reply_text("Game notification removed.")


async def daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_files_exist()
    store = load_json(SUBSCRIBERS_FILE, {"chat_ids": [], "users": {}})
    subs = store.get("chat_ids", [])
    offer_subs: Dict[str, Dict[str, Any]] = load_json(SUBSCRIBERS_FILE, {"offer_subs": {}}).get("offer_subs", {})
    if not subs:
        subs = []
    # Build a union of chats to process: regular daily + those who have per-offer subs
    chat_ids_to_process = set(map(int, subs)) | set(map(int, offer_subs.keys()))

    # For each chat, check per-offer subscriptions: if an upcoming subscribed game is now free, notify once
    for chat_id in chat_ids_to_process:
        prefs = get_user_prefs(chat_id)
        try:
            current = await fetch_free_games(locale=prefs["locale"], country=prefs["country"])
        except Exception:
            current = []
        # Map current free offers by id
        free_ids = set()
        for el in current:
            oid = get_offer_id(el)
            if oid:
                free_ids.add(oid)

        # Notify for offers that became free
        user_offer_subs = offer_subs.get(str(chat_id), {})
        any_sent = False
        for oid, meta in user_offer_subs.items():
            if free_ids and oid in free_ids and not meta.get("notified"):
                title = meta.get("title") or oid
                url = f"https://store.epicgames.com/en-US/p/{meta.get('pageSlug')}" if meta.get("pageSlug") else "https://store.epicgames.com/"
                try:
                    await context.bot.send_message(chat_id=chat_id, text=f"Now free: {title}\n{url}")
                    meta["notified"] = True
                    any_sent = True
                except Exception:
                    pass
        if any_sent:
            # Persist notifications state
            data = load_json(SUBSCRIBERS_FILE, {"chat_ids": [], "users": {}, "offer_subs": {}})
            data["offer_subs"][str(chat_id)] = user_offer_subs
            save_json(SUBSCRIBERS_FILE, data)

    # Keep existing daily digest for regular subscribers
    for chat_id in subs:
        try:
            await send_free_games(chat_id, context)
        except Exception:
            # Ignore errors to continue sending to others
            pass


def main() -> None:
    load_dotenv()
    
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN not found in environment variables")
        print("Please create a .env file with your bot token:")
        print("TELEGRAM_BOT_TOKEN=your_bot_token_here")
        return

    ensure_files_exist()

    # Start lightweight HTTP server for Koyeb health checks
    start_health_server_in_background()

    app = Application.builder().token(token).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("freegames", freegames_cmd))
    app.add_handler(CommandHandler("upcoming", upcoming_cmd))
    app.add_handler(CommandHandler("subscribe", subscribe_cmd))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe_cmd))
    app.add_handler(CommandHandler("setlocale", setlocale_cmd))
    app.add_handler(CommandHandler("setcountry", setcountry_cmd))
    from telegram.ext import CallbackQueryHandler
    app.add_handler(CallbackQueryHandler(on_callback))

    # Fallback: reply to any text with a hint
    async def _fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("Try /freegames, /subscribe or /unsubscribe")

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _fallback))

    # Schedule a daily job at 10:00 UTC (if JobQueue is available)
    if app.job_queue is not None:
        app.job_queue.run_daily(daily_job, time=time(hour=10, minute=0, tzinfo=timezone.utc))
    else:
        print("Warning: JobQueue is not available. Daily reminders are disabled.\n"
              "Install with: pip install \"python-telegram-bot[job-queue]\"")

    print("Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
