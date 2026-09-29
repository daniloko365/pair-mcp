import json

from starlette.testclient import TestClient

from pair_core.providers import RoutingError
from pair_core.service import Service
from pair_core.store import Store


class NoSecretsVault:
    def has(self, key):
        return False


class QuietCli:
    async def status(self):
        return {}


class BrokenCatalog:
    def __init__(self, error):
        self.error = error

    async def models(self, provider):
        raise self.error


def make_client(tmp_path, error):
    store = Store(tmp_path)
    config = store.config()
    config["providers"] = [{"id": "p", "name": "p", "baseUrl": "https://example.com/v1"}]
    store.save_config(config)
    service = Service(store, NoSecretsVault(), cli=QuietCli(), catalog=BrokenCatalog(error))
    return TestClient(service.app("public-fixture-local-token"), base_url="http://127.0.0.1")


def test_safe_catalog_code_reaches_native_client(tmp_path):
    api = make_client(tmp_path, RoutingError("catalog_http_401"))
    response = api.post("/providers/p/models", json={}, headers={"Authorization": "Bearer public-fixture-local-token"})
    assert response.status_code == 502
    assert response.json()["error"] == "catalog_http_401"


def test_catalog_transport_codes_reach_native_without_private_details(tmp_path):
    for code in ("catalog_connection_error", "catalog_timeout"):
        api = make_client(tmp_path, RoutingError(code, partial={"text": "private upstream detail"}))
        response = api.post("/providers/p/models", json={}, headers={"Authorization": "Bearer public-fixture-local-token"})
        assert response.status_code == 502
        assert response.json()["error"] == code
        assert "private upstream detail" not in json.dumps(response.json())


def test_catalog_error_does_not_expand_authorization(tmp_path):
    api = make_client(tmp_path, RoutingError("catalog_unavailable"))
    assert api.post("/providers/p/models", json={}).status_code == 401
    assert api.post("/providers/p/models", json={}, headers={"Authorization": "Bearer public-fixture-local-token", "Origin": "https://example.com"}).status_code == 403


def test_arbitrary_failure_text_and_partial_result_stay_private(tmp_path):
    marker = "public-test-marker-do-not-reflect"
    for error in (RuntimeError(marker), RoutingError("catalog_unavailable", partial={"text": marker})):
        api = make_client(tmp_path, error)
        response = api.post("/providers/p/models", json={}, headers={"Authorization": "Bearer public-fixture-local-token"})
        assert marker not in json.dumps(response.json())
