"""Short-lived, private direct upload for files larger than Telegram's download limit."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import Settings
from .store import Store

MAX_UPLOAD_BYTES = 500 * 1024 * 1024
SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v", ".webm", ".mp3", ".wav", ".m4a", ".ogg"}


def _encode_token(secret: str, reel_id: int, chat_id: int, expires: int) -> str:
    payload = json.dumps({"reel": reel_id, "chat": chat_id, "exp": expires}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _decode_token(secret: str, token: str) -> dict:
    try:
        encoded, signature = token.rsplit(".", 1)
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if int(payload["exp"]) < time.time():
            raise ValueError("expired")
        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=403, detail="Invalid or expired upload link") from exc


def make_upload_link(settings: Settings, reel_id: int, chat_id: int) -> str:
    if not settings.public_base_url.startswith("https://") or len(settings.upload_secret) < 32:
        raise ValueError("HTTPS PUBLIC_BASE_URL and UPLOAD_SECRET are required")
    token = _encode_token(settings.upload_secret, reel_id, chat_id, int(time.time()) + 3600)
    return f"{settings.public_base_url}/upload/{token}"


app = FastAPI(title="Private reel asset upload", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/upload/{token}", response_class=HTMLResponse)
async def upload_page(token: str) -> str:
    settings = Settings.from_env()
    if not settings.upload_secret:
        raise HTTPException(status_code=503, detail="Upload is not configured")
    _decode_token(settings.upload_secret, token)
    return """<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>آپلود فایل پروژه</title><style>body{font:18px sans-serif;max-width:540px;margin:10vh auto;padding:20px}button{padding:12px 24px}</style><h1>آپلود فایل پروژه</h1><p>فایل عکس یا ویدیوی خام را انتخاب کنید. بعد از پایان، به تلگرام برگردید و «فایل‌ها تمام شد» را بزنید.</p><input id="file" type="file" accept="image/*,video/*,audio/*"><button onclick="send()">آپلود</button><p id="result"></p><script>async function send(){const f=document.getElementById('file').files[0];if(!f)return;const out=document.getElementById('result');out.textContent='در حال آپلود…';try{const r=await fetch(location.href,{method:'PUT',headers:{'X-File-Name':encodeURIComponent(f.name)},body:f});const d=await r.json();out.textContent=r.ok?'فایل دریافت شد. می‌توانید فایل بعدی را انتخاب کنید.':(d.detail||'خطا در آپلود');}catch(e){out.textContent='اتصال برقرار نشد؛ دوباره تلاش کنید.'}}</script></html>"""


@app.put("/upload/{token}")
async def upload_file(token: str, request: Request) -> JSONResponse:
    settings = Settings.from_env()
    if not settings.upload_secret:
        raise HTTPException(status_code=503, detail="Upload is not configured")
    payload = _decode_token(settings.upload_secret, token)
    store = Store(settings.data_dir / "reelbot.sqlite3")
    try:
        reel = store.get_reel(int(payload["reel"]))
        if not reel or reel["status"] != "collecting" or int(reel["chat_id"]) != int(payload["chat"]):
            raise HTTPException(status_code=403, detail="This project is no longer accepting files")
        raw_name = request.headers.get("x-file-name", "")
        from urllib.parse import unquote
        suffix = Path(unquote(raw_name)).suffix.lower()
        if suffix not in SUFFIXES:
            raise HTTPException(status_code=415, detail="Unsupported file type")
        kind = "photo" if suffix in {".jpg", ".jpeg", ".png", ".webp"} else "video" if suffix in {".mp4", ".mov", ".m4v", ".webm"} else "audio"
        target_dir = settings.data_dir / "raw" / str(reel["id"])
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{uuid.uuid4().hex}{suffix}"
        temporary = target.with_name(target.name + ".uploading")
        size = 0
        try:
            with temporary.open("wb") as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="File exceeds 500 MB")
                    output.write(chunk)
            if size == 0:
                raise HTTPException(status_code=400, detail="Empty file")
            os.replace(temporary, target)
            store.add_asset(reel["id"], kind, str(target))
        finally:
            temporary.unlink(missing_ok=True)
        return JSONResponse({"ok": True, "size": size})
    finally:
        store.close()
