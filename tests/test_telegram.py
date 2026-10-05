import asyncio

import httpx
import pytest

from reelbot.telegram import TelegramAPI, TelegramError


def test_download_preserves_exact_bytes_and_checks_reported_size(tmp_path):
    payload = b"original-photo-or-video-bytes"
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/source.jpg", "file_size": len(payload)}})
        return httpx.Response(200, content=payload)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        api = TelegramAPI("test-token", client=client)
        path = await api.download("file-id", tmp_path / "source.jpg")
        assert path.read_bytes() == payload
        assert calls == 2
        await client.aclose()

    asyncio.run(run())


def test_download_removes_partial_file_on_size_mismatch(tmp_path):
    def handler(request):
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "source.jpg", "file_size": 10}})
        return httpx.Response(200, content=b"short")

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        api = TelegramAPI("test-token", client=client)
        destination = tmp_path / "partial.jpg"
        with pytest.raises(TelegramError, match="size did not match"):
            await api.download("file-id", destination)
        assert not destination.exists()
        await client.aclose()

    asyncio.run(run())


def test_send_document_uploads_same_file_bytes(tmp_path):
    payload = b"approved-mp4-bytes"

    def handler(request):
        body = request.read()
        assert b"name=\"document\"" in body
        assert payload in body
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        api = TelegramAPI("test-token", client=client)
        path = tmp_path / "reel.mp4"
        path.write_bytes(payload)
        result = await api.send_document(42, path, "final")
        assert result["message_id"] == 99
        await client.aclose()

    asyncio.run(run())
