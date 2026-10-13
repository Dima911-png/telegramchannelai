"""Regression tests for the no-key publishing path."""
import pytest

import channel_ai


@pytest.mark.asyncio
async def test_process_post_uses_safe_fallback_without_provider_key(monkeypatch):
    monkeypatch.setattr(channel_ai, "POLLINATIONS_KEY", "")
    post = {
        "text": "  A  news   item with several spaces.  ",
        "source": "Example source",
        "source_username": "example",
        "msg_id": "123",
    }

    result = await channel_ai.process_post(None, post, {})

    assert result is not None
    assert "Коротко о главном" in result["text"]
    assert "A news item with several spaces." in result["text"]
    assert "https://t.me/example/123" in result["text"]
    assert result["image_prompt"] == ""
    assert result["poll_data"] == ""


@pytest.mark.asyncio
async def test_process_post_skips_empty_source_text():
    result = await channel_ai.process_post(None, {"text": "  "}, {})
    assert result is None


@pytest.mark.asyncio
async def test_process_post_falls_back_when_ai_provider_fails(monkeypatch):
    monkeypatch.setattr(channel_ai, "POLLINATIONS_KEY", "test-key")

    async def failed_ai_call(*args, **kwargs):
        return None

    monkeypatch.setattr(channel_ai, "ai_call", failed_ai_call)
    result = await channel_ai.process_post(
        None, {"text": "A source report.", "source": "Example"}, {}
    )

    assert result is not None
    assert "A source report." in result["text"]
    assert "Источник: Example" in result["text"]
    assert result["image_prompt"] == ""
