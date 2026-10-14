"""Bot API pipeline for public Telegram source channels.

This module avoids a user-account Telethon session: it reads public t.me/s pages
and publishes through the official Telegram Bot API. Private/restricted sources
are not supported by this mode.
"""
import asyncio
import html
import os
import re
import time
from datetime import datetime, timezone, timedelta

import aiohttp

from channel_ai import (
    DEFAULT_SOURCES, MAX_POSTS, SCAN_HOURS, SCAN_LIMIT, DEDUP_HOURS,
    PUBLISH_DELAY, POLLINATIONS_KEY, process_post, is_duplicate,
    load_hashes, save_hashes, generate_image,
)

def _configured_sources():
    """Prefer active sources added in the dashboard database; use defaults otherwise."""
    try:
        from web.db import get_sources
        rows = get_sources() or []
        names = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("is_active", 1):
                continue
            name = row.get("username")
            if name:
                names.append(str(name).strip().lstrip("@"))
        # Honour dashboard sources even when none are active; do not silently
        # scan unrelated defaults when the user configured their own list.
        if rows:
            return list(dict.fromkeys(names))
    except Exception as exc:
        print(f"  Could not load dashboard sources; using DEFAULT_SOURCES: {exc}")
    return [str(s).strip().lstrip("@") for s in DEFAULT_SOURCES if str(s).strip()]


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
TEXT_MODEL = os.getenv("TEXT_MODEL", "claude")
PIPELINE_MODE = os.getenv("PIPELINE_MODE", "semi-auto")


def _plain_text(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"</div\s*>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    fragment = html.unescape(fragment)
    return re.sub(r"[ \t\r\f\v]+", " ", fragment).strip()


async def scan_public_sources(session, channels, hours=SCAN_HOURS):
    """Read recent public posts from Telegram's public web preview."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    posts = []
    for raw_name in channels:
        username = raw_name.strip().lstrip("@")
        if not username:
            continue
        url = f"https://t.me/s/{username}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                if resp.status != 200:
                    print(f"  Source @{username}: public preview unavailable (HTTP {resp.status})")
                    continue
                page = await resp.text()
            # Split at each outer message wrapper instead of matching nested
            # HTML divs with a regex, which can swallow or truncate posts.
            starts = [m.start() for m in re.finditer(r'<div class="tgme_widget_message_wrap', page)]
            blocks = [page[a:(starts[i + 1] if i + 1 < len(starts) else len(page))]
                      for i, a in enumerate(starts)]
            for block in blocks[-SCAN_LIMIT:]:
                post_match = re.search(r'data-post="([^"]+)"', block)
                text_match = re.search(
                    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                    block, flags=re.S
                )
                date_match = re.search(r'<time[^>]*datetime="([^"]+)"', block)
                if not text_match:
                    continue
                text = _plain_text(text_match.group(1))
                if len(text) <= 80:
                    continue
                try:
                    published = datetime.fromisoformat(date_match.group(1).replace("Z", "+00:00")) if date_match else None
                except ValueError:
                    published = None
                if published and published < since:
                    continue
                msg_id = None
                if post_match and "/" in post_match.group(1):
                    msg_id = post_match.group(1).rsplit("/", 1)[-1]
                posts.append({
                    "text": text[:500],
                    "source": f"@{username}",
                    "source_username": username,
                    "msg_id": msg_id,
                    "views": 0,
                })
        except Exception as exc:
            print(f"  Source @{username} scan failed: {exc}")
        await asyncio.sleep(0.3)
    posts.reverse()
    return posts


async def _bot_api(session, method, *, data=None):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing in Render Environment")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    async with session.post(url, data=data,
                            timeout=aiohttp.ClientTimeout(total=60)) as resp:
        result = await resp.json(content_type=None)
        if resp.status != 200 or not result.get("ok"):
            raise RuntimeError(f"Telegram Bot API {method} failed: {result.get('description', resp.status)}")
        return result["result"]


async def publish_post_bot(session, destination, result):
    text = result.get("text", "").strip()
    if not text:
        return None
    # Bot API supports HTML formatting. Keep the message under Telegram's text limit.
    if len(text) > 4000:
        text = text[:3990].rsplit(" ", 1)[0] + "…"
    # Telegram HTML parsing can fail on model-generated malformed markup.
    # Retry as plain text rather than silently losing a generated post.
    plain_text = re.sub(r"<br\\s*/?>", "\\n", text, flags=re.I)
    plain_text = re.sub(r"</(p|div|li|h[1-6])\\s*>", "\\n", plain_text, flags=re.I)
    plain_text = html.unescape(re.sub(r"<[^>]+>", "", plain_text))
    image_path = None
    try:
        if result.get("image_prompt"):
            image_path = await generate_image(result["image_prompt"])
        if image_path and os.path.exists(image_path) and os.path.getsize(image_path) <= 9_500_000:
            with open(image_path, "rb") as photo:
                form = aiohttp.FormData()
                form.add_field("chat_id", destination)
                form.add_field("caption", text[:1024])
                form.add_field("parse_mode", "HTML")
                form.add_field("photo", photo, filename="post.jpg", content_type="image/jpeg")
                sent = await _bot_api(session, "sendPhoto", data=form)
            if len(text) > 1024:
                await _bot_api(session, "sendMessage", data={
                    "chat_id": destination, "text": text, "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                })
        else:
            try:
                sent = await _bot_api(session, "sendMessage", data={
                    "chat_id": destination, "text": text, "parse_mode": "HTML",
                    "disable_web_page_preview": "false",
                })
            except RuntimeError as exc:
                if "parse entities" not in str(exc).lower() and "can't parse" not in str(exc).lower():
                    raise
                sent = await _bot_api(session, "sendMessage", data={
                    "chat_id": destination, "text": plain_text,
                    "disable_web_page_preview": "false",
                })
        return sent.get("message_id")
    finally:
        if image_path and os.path.exists(image_path):
            try:
                os.unlink(image_path)
            except OSError:
                pass


async def run_cycle(channel_key="default", max_posts=MAX_POSTS):
    """Scan public sources, generate posts, and publish with a Telegram bot."""
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is missing.")
        return []
    if not CHANNEL_ID:
        print("ERROR: CHANNEL_ID is missing.")
        return []
    sources = _configured_sources()
    if not sources:
        print("ERROR: DEFAULT_SOURCES is empty. Set it to public source usernames.")
        return []

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Bot API pipeline starting; sources={len(sources)}")
    load_hashes()
    async with aiohttp.ClientSession() as session:
        posts = await scan_public_sources(session, sources)
        print(f"  Found {len(posts)} public source posts")
        # Deduplication checks mutate the shared hash set. Keep a snapshot so a
        # post is not permanently marked as handled before it is actually published.
        import channel_ai as _channel_ai
        old_hashes = set(_channel_ai._published_hashes)
        old_tokens = dict(_channel_ai._hash_tokens)
        unique = [p for p in posts if not is_duplicate(p["text"])]
        _channel_ai._published_hashes = old_hashes
        _channel_ai._hash_tokens = old_tokens
        selected = unique[:max_posts]
        if not selected:
            save_hashes()
            print("  No new posts to process.")
            return []

        prompt_config = {
            "system_prompt": (
                "Ты редактор русскоязычного Telegram-канала. Перепиши новость на русском языке "
                "ясно, интересно и естественно. Не выдумывай факты. Сохраняй важные оговорки. "
                "Используй короткий заголовок, абзацы и уместные эмодзи. Форматирование Telegram HTML. "
                "Не копируй исходный текст дословно. Добавь ссылку на источник, если она дана."
            ),
            "language": "ru",
            "search_topics": ["актуальные новости"],
            "sources": sources,
            "channel_id": CHANNEL_ID,
        }
        results = []
        for i, post in enumerate(selected):
            print(f"  [{i+1}/{len(selected)}] Generating: {post['text'][:60]}...")
            processed = await process_post(session, post, prompt_config)
            if processed:
                processed["_source_text"] = post["text"]
                results.append(processed)
            await asyncio.sleep(1)
        published = []
        for result in results:
            try:
                msg_id = await publish_post_bot(session, CHANNEL_ID, result)
                published.append(result)
                # Commit this source fingerprint only after Telegram confirms success.
                is_duplicate(result.get("_source_text", result.get("text", "")))
                print(f"  Published via Bot API: message_id={msg_id}")
            except Exception as exc:
                print(f"  Publish failed: {exc}")
            await asyncio.sleep(PUBLISH_DELAY)
    save_hashes()
    print(f"Pipeline complete: {len(published)} post(s) published.")
    return published


async def run_all_channels(max_posts=MAX_POSTS):
    return {"default": await run_cycle(max_posts=max_posts)}


if __name__ == "__main__":
    asyncio.run(run_all_channels())
