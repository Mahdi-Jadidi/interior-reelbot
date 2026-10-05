from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx


class TelegramError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self.token = token
        self._client = client or httpx.AsyncClient(timeout=65)
        self._owns_client = client is None
        self.base = f"https://api.telegram.org/bot{token}"

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._client.post(f"{self.base}/{method}", json=payload or {})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise TelegramError(f"Telegram {method} HTTP request failed") from None
        body = response.json()
        if not body.get("ok"):
            raise TelegramError(body.get("description", "Telegram request failed"))
        return body["result"]

    async def updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        return await self.call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
        )

    async def send_message(
        self, chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None
    ) -> dict:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in row]
                    for row in buttons
                ]
            }
        return await self.call("sendMessage", payload)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def download(self, file_id: str, destination: Path) -> Path:
        info = await self.call("getFile", {"file_id": file_id})
        file_path = info["file_path"]
        expected_size = info.get("file_size")
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        received = 0
        try:
            async with self._client.stream("GET", url) as response:
                response.raise_for_status()
                with destination.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > 2_000_000_000:
                            raise TelegramError("Telegram file exceeds the local 2 GB safety limit")
                        output.write(chunk)
            if expected_size is not None and received != int(expected_size):
                raise TelegramError("Telegram download size did not match the original file")
        except TelegramError:
            destination.unlink(missing_ok=True)
            raise
        except httpx.HTTPError:
            destination.unlink(missing_ok=True)
            raise TelegramError("Telegram file download failed") from None
        return destination

    async def send_video(self, chat_id: int, path: Path, caption: str = "", buttons=None) -> dict:
        data: dict[str, str] = {"chat_id": str(chat_id), "caption": caption}
        if buttons:
            data["reply_markup"] = json.dumps(
                {"inline_keyboard": [
                    [{"text": label, "callback_data": value} for label, value in row]
                    for row in buttons
                ]}, ensure_ascii=False
            )
        with path.open("rb") as video:
            try:
                response = await self._client.post(
                    f"{self.base}/sendVideo",
                    data=data,
                    files={"video": (path.name, video, "video/mp4")},
                    timeout=180,
                )
                response.raise_for_status()
            except httpx.HTTPError:
                raise TelegramError("Telegram video upload failed") from None
        body = response.json()
        if not body.get("ok"):
            raise TelegramError(body.get("description", "Telegram upload failed"))
        return body["result"]

    async def send_document(self, chat_id: int, path: Path, caption: str = "") -> dict:
        """Send the exact MP4 bytes as a file after client approval."""
        with path.open("rb") as document:
            try:
                response = await self._client.post(
                    f"{self.base}/sendDocument",
                    data={"chat_id": str(chat_id), "caption": caption},
                    files={"document": (path.name, document, "video/mp4")},
                    timeout=300,
                )
                response.raise_for_status()
            except httpx.HTTPError:
                raise TelegramError("Telegram original-file upload failed") from None
        body = response.json()
        if not body.get("ok"):
            raise TelegramError(body.get("description", "Telegram original-file upload failed"))
        return body["result"]

    async def send_photo(self, chat_id: int, path: Path, caption: str = "") -> dict:
        with path.open("rb") as photo:
            try:
                response = await self._client.post(
                    f"{self.base}/sendPhoto", data={"chat_id": str(chat_id), "caption": caption},
                    files={"photo": (path.name, photo, "image/jpeg")}, timeout=90,
                )
                response.raise_for_status()
            except httpx.HTTPError:
                raise TelegramError("Telegram cover upload failed") from None
        body = response.json()
        if not body.get("ok"):
            raise TelegramError(body.get("description", "Telegram cover upload failed"))
        return body["result"]
