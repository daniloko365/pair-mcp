import json

from starlette.testclient import TestClient

from pair_core.service import Service
from pair_core.store import Store


class FixtureVault:
    def __init__(self): self.keys = {}
    def set(self, key, value): self.keys[key] = value
    def get(self, key): return self.keys.get(key)
    def has(self, key): return key in self.keys
    def delete(self, key): self.keys.pop(key, None)


class FixtureCli:
    async def status(self): return {"codex": {"available": False, "quotas": None}}


def client(tmp_path):
    store = Store(tmp_path)
    vault = FixtureVault()
    return TestClient(Service(store, vault, cli=FixtureCli()).app("fixture-local-token"), base_url="http://127.0.0.1"), store, vault


def auth(): return {"Authorization": "Bearer fixture-local-token"}


def test_no_auth_and_browser_origin_are_rejected(tmp_path):
    api, _, _ = client(tmp_path)
    assert api.get("/state").status_code == 401
    assert api.get("/state", headers={**auth(), "Origin": "https://untrusted.example"}).status_code == 403
    assert api.get("/state", headers={**auth(), "Host": "untrusted.example"}).status_code == 403
    assert api.get("/state", headers=auth()).status_code == 200


def test_key_is_write_only_and_never_in_config_or_state(tmp_path):
    api, store, vault = client(tmp_path)
    cfg = store.config()
    cfg["providers"] = [{"id": "fixture", "name": "Fixture", "baseUrl": "https://example.com/v1"}]
    assert api.put("/config", headers=auth(), json=cfg).status_code == 200
    key = "fixture-private-not-real"
    response = api.post("/providers/fixture/key", headers=auth(), json={"key": key})
    assert response.json() == {"keyPresent": True}
    assert key not in api.get("/state", headers=auth()).text
    assert key not in json.dumps(store.config())
    assert vault.get("fixture") == key
    assert api.delete("/providers/fixture/key", headers=auth()).json() == {"keyPresent": False}


def test_stale_save_and_invalid_key_resource(tmp_path):
    api, store, _ = client(tmp_path)
    cfg = store.config()
    assert api.put("/config", headers=auth(), json=cfg).status_code == 200
    assert api.put("/config", headers=auth(), json=cfg).status_code == 409
    assert api.post("/providers/absent/key", headers=auth(), json={"key": "fixture"}).status_code == 404


def test_json_required_and_unsafe_config_cannot_save(tmp_path):
    api, store, _ = client(tmp_path)
    assert api.put("/config", headers=auth(), content="bad").status_code == 415
    cfg = store.config()
    cfg["providers"] = [{"id": "p", "name": "p", "baseUrl": "https://example.com?key=bad"}]
    assert api.put("/config", headers=auth(), json=cfg).status_code == 400
    assert store.config()["providers"] == []
