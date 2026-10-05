import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from reelbot.config import Settings
from reelbot.store import Store
from reelbot.upload import _decode_token, _encode_token, app, make_upload_link


def test_upload_link_is_signed_and_expires(tmp_path):
    settings = Settings("bot", "key", tmp_path, frozenset({42}),
                        public_base_url="https://example.test", upload_secret="secret" * 8)
    link = make_upload_link(settings, 7, 42)
    token = link.split("/upload/", 1)[1]
    assert _decode_token(settings.upload_secret, token)["reel"] == 7
    with pytest.raises(HTTPException):
        _decode_token(settings.upload_secret, token + "x")
    expired = _encode_token(settings.upload_secret, 7, 42, int(time.time()) - 10)
    with pytest.raises(HTTPException):
        _decode_token(settings.upload_secret, expired)


def test_upload_link_requires_https(tmp_path):
    settings = Settings("bot", "key", tmp_path, frozenset({42}),
                        public_base_url="http://example.test", upload_secret="secret")
    with pytest.raises(ValueError):
        make_upload_link(settings, 7, 42)


def test_private_upload_attaches_to_collecting_reel(tmp_path, monkeypatch):
    monkeypatch.setenv("REELBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("UPLOAD_SECRET", "private-secret-that-is-at-least-thirty-two-characters")
    store = Store(tmp_path / "reelbot.sqlite3")
    reel = store.create_reel(42, "fa")
    token = _encode_token("private-secret-that-is-at-least-thirty-two-characters", reel["id"], 42, int(time.time()) + 100)
    client = TestClient(app)
    response = client.put(f"/upload/{token}", content=b"photo-bytes", headers={"X-File-Name": "room.jpg"})
    assert response.status_code == 200
    assert store.list_assets(reel["id"])[0]["kind"] == "photo"
    store.update_reel(reel["id"], status="awaiting_plan_approval")
    response = client.put(f"/upload/{token}", content=b"more", headers={"X-File-Name": "other.jpg"})
    assert response.status_code == 403
    store.close()
