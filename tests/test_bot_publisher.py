"""Tests for Telegram Bot API publishing without live network access."""
import pytest

import channel_ai_bot


@pytest.mark.asyncio
async def test_publish_text_post_uses_html_and_returns_message_id(monkeypatch):
    calls = []

    async def fake_bot_api(session, method, *, data=None):
        calls.append((method, data))
        return {"message_id": 321}

    monkeypatch.setattr(channel_ai_bot, "BOT_TOKEN", "test-token")
    monkeypatch.setattr(channel_ai_bot, "_bot_api", fake_bot_api)
    monkeypatch.setattr(channel_ai_bot, "generate_image", _no_image)

    result = await channel_ai_bot.publish_post_bot(
        object(), "@destination", {"text": "<b>Headline</b>", "image_prompt": ""}
    )

    assert result == 321
    assert calls[0][0] == "sendMessage"
    assert calls[0][1]["parse_mode"] == "HTML"
    assert calls[0][1]["chat_id"] == "@destination"


@pytest.mark.asyncio
async def test_publish_retries_plain_text_on_html_parse_error(monkeypatch):
    calls = []

    async def fake_bot_api(session, method, *, data=None):
        calls.append(data)
        if len(calls) == 1:
            raise RuntimeError("Telegram Bot API sendMessage failed: can't parse entities")
        return {"message_id": 654}

    monkeypatch.setattr(channel_ai_bot, "BOT_TOKEN", "test-token")
    monkeypatch.setattr(channel_ai_bot, "_bot_api", fake_bot_api)
    monkeypatch.setattr(channel_ai_bot, "generate_image", _no_image)

    result = await channel_ai_bot.publish_post_bot(
        object(), "@destination", {"text": "<b>broken", "image_prompt": ""}
    )

    assert result == 654
    assert calls[0]["parse_mode"] == "HTML"
    assert "parse_mode" not in calls[1]
    assert calls[1]["text"] == "broken"


async def _no_image(prompt, *args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_photo_caption_retries_without_html_when_entities_are_invalid(monkeypatch, tmp_path):
    import os

    image = tmp_path / "sample.jpg"
    image.write_bytes(b"fake-image")
    calls = []

    async def fake_generate_image(prompt):
        return str(image)

    async def fake_bot_api(session, method, *, data=None):
        calls.append((method, data))
        if len(calls) == 1:
            raise RuntimeError("Telegram Bot API sendPhoto failed: can't parse entities")
        return {"message_id": 987}

    monkeypatch.setattr(channel_ai_bot, "BOT_TOKEN", "test-token")
    monkeypatch.setattr(channel_ai_bot, "_bot_api", fake_bot_api)
    monkeypatch.setattr(channel_ai_bot, "generate_image", fake_generate_image)
    monkeypatch.setattr(channel_ai_bot.os.path, "getsize", lambda path: 100)

    result = await channel_ai_bot.publish_post_bot(
        object(), "@destination", {"text": "<b>broken", "image_prompt": "test"}
    )

    assert result == 987
    assert calls[0][0] == "sendPhoto"
    assert calls[1][0] == "sendPhoto"
    assert image.exists() is False
