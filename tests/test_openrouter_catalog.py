import httpx2
import pytest

from pair_core.providers import Catalog


class PublicVault:
    def get(self, provider_id):
        return "public-test-key"


@pytest.mark.asyncio
async def test_openrouter_full_catalog_includes_media_metadata():
    def handler(request):
        assert request.url.params.get("output_modalities") == "all"
        return httpx2.Response(200, json={"data": [{"id": "text", "architecture": {"output_modalities": ["text"]}}, {"id": "image", "architecture": {"output_modalities": ["image"]}}]})
    rows = await Catalog(PublicVault(), transport=httpx2.MockTransport(handler)).models({"id": "public", "protocol": "openai", "baseUrl": "https://openrouter.ai/api/v1", "manualModels": []})
    assert {row["id"] for row in rows} == {"text", "image"}
    assert next(row for row in rows if row["id"] == "image")["capabilities"]["interactiveCompatible"] is False


@pytest.mark.asyncio
async def test_custom_compatible_endpoint_does_not_receive_vendor_query():
    def handler(request):
        assert not request.url.query
        return httpx2.Response(200, json={"data": []})
    await Catalog(PublicVault(), transport=httpx2.MockTransport(handler)).models({"id": "public", "protocol": "openai", "baseUrl": "https://openrouter.ai.custom.example/v1", "manualModels": []})
