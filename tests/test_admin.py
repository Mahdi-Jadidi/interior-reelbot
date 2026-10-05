import json

import pytest

from reelbot.admin import retry_reel
from reelbot.store import Store


def test_retry_requires_current_approval_and_exception(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    reel = store.create_reel(1, "fa")
    reel = store.update_reel(reel["id"], plan_json=json.dumps({"script": "a"}),
                             plan_hash="hash", script="a", status="needs_operator")
    with pytest.raises(ValueError):
        retry_reel(store, reel["id"])
    store.set_approval(reel["id"], "hash")
    first = retry_reel(store, reel["id"])
    assert first["kind"] == "render"
    assert store.get_reel(reel["id"])["status"] == "queued"
    with pytest.raises(ValueError):
        retry_reel(store, reel["id"])
    store.close()
