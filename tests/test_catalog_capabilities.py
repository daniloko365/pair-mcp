"""Public provider-shaped metadata fixtures; no requests to a real provider."""
import httpx2
import pytest

from pair_core.providers import Catalog, normalize_model


def openrouter_model(mid="example/text-model", **fields):
    return {
        "id": mid,
        "name": "Example: Text Model",
        "context_length": 128000,
        "architecture": {
            "modality": "text+image->text",
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
            "tokenizer": "Other",
            "instruct_type": None,
        },
        "supported_parameters": ["max_tokens", "temperature", "reasoning"],
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        "top_provider": {"context_length": 128000, "max_completion_tokens": 8192},
        **fields,
    }


def test_text_model_preserves_advertised_input_and_output_modalities():
    model = normalize_model(openrouter_model())
    caps = model["capabilities"]
    assert caps["inputModalities"] == ["text", "image"]
    assert caps["outputModalities"] == ["text"]
    assert caps["textChatCompatible"] is True
    assert caps["batchVariant"] is False
    assert caps["interactiveCompatible"] is True
    assert caps["compatibilityReason"] == "text_chat_advertised"
    assert caps["chat"] is True
    assert caps["reasoningSupported"] is True
    assert caps["reasoningKnown"] is True
    assert caps["reasoningEnumKnown"] is False
    assert model["reasoning"] == []


@pytest.mark.parametrize("architecture", [
    {"input_modalities": ["text"], "output_modalities": ["image"]},
    {"input_modalities": ["text"], "output_modalities": ["audio", "video"]},
    {"input_modalities": ["image"], "output_modalities": ["text"]},
])
def test_explicit_non_text_modalities_establish_incompatible_model(architecture):
    caps = normalize_model(openrouter_model(architecture=architecture))["capabilities"]
    assert caps["inputModalities"] == architecture["input_modalities"]
    assert caps["outputModalities"] == architecture["output_modalities"]
    assert caps["textChatCompatible"] is False
    assert caps["interactiveCompatible"] is False
    assert caps["compatibilityReason"] == "non_text_modalities"
    assert caps["chat"] is False


@pytest.mark.parametrize("raw", [
    {"id": "manual"},
    {"id": "sdk-model", "object": "model", "owned_by": "example"},
    {"id": "partial", "architecture": {"output_modalities": ["text"]}},
    {"id": "empty", "architecture": {"input_modalities": [], "output_modalities": []}},
    {"id": "malformed", "architecture": {"input_modalities": "text", "output_modalities": None}},
    {"id": "nullable", "architecture": None},
])
def test_unknown_modalities_remain_unknown_and_do_not_disable_manual_models(raw):
    caps = normalize_model(raw)["capabilities"]
    assert caps["textChatCompatible"] is None
    assert caps["interactiveCompatible"] is None
    assert caps["compatibilityReason"] == "metadata_unknown"
    assert caps["chat"] is True
    assert caps["inputModalities"] is None
    if raw["id"] != "partial":
        assert caps["outputModalities"] is None


@pytest.mark.parametrize("fields, supported, known, enum_known", [
    ({}, False, False, False),
    ({"supported_parameters": None}, False, False, False),
    ({"supported_parameters": "reasoning"}, False, False, False),
    ({"reasoning": None}, False, False, False),
    ({"supported_parameters": []}, False, True, False),
    ({"reasoning": False}, False, True, False),
    ({"reasoning": []}, False, True, True),
    ({"reasoning": True}, True, True, False),
    ({"supported_parameters": ["reasoning_effort"]}, True, True, False),
    ({"reasoning": ["low", "high"]}, True, True, True),
])
def test_reasoning_distinguishes_missing_unsupported_supported_and_enum(fields, supported, known, enum_known):
    model = normalize_model({"id": "example", **fields})
    caps = model["capabilities"]
    assert caps["reasoningSupported"] is supported
    assert caps["reasoningKnown"] is known
    assert caps["reasoningEnumKnown"] is enum_known
    assert model["reasoning"] == (fields.get("reasoning") if enum_known else [])


@pytest.mark.parametrize("mid, batch", [
    ("example/text:batch", True),
    ("example/text:BATCH", True),
    ("example/batch-model", False),
    ("example/text:batch:free", False),
])
def test_batch_suffix_is_an_explicit_variant_hint_and_full_metadata_is_preserved(mid, batch):
    model = normalize_model(openrouter_model(mid))
    caps = model["capabilities"]
    assert model["id"] == mid
    assert caps["batchVariant"] is batch
    assert caps["interactiveCompatible"] is not batch
    assert caps["compatibilityReason"] == ("batch_variant" if batch else "text_chat_advertised")
    assert caps["textChatCompatible"] is True
    assert caps["chat"] is True


def test_manual_batch_variant_stays_in_catalog_with_unknown_modalities():
    model = normalize_model({"id": "owner/manual:batch"}, source="manual")
    caps = model["capabilities"]
    assert model["source"] == "manual"
    assert caps["inputModalities"] is None
    assert caps["outputModalities"] is None
    assert caps["textChatCompatible"] is None
    assert caps["batchVariant"] is True
    assert caps["interactiveCompatible"] is False
    assert caps["compatibilityReason"] == "batch_variant"
    assert caps["chat"] is True


@pytest.mark.asyncio
async def test_sdk_catalog_keeps_non_text_and_batch_shaped_models_and_selected_metadata():
    rows = [
        openrouter_model("example/text"),
        openrouter_model("example/image", architecture={"input_modalities": ["text"], "output_modalities": ["image"]}),
        openrouter_model("example/text:batch"),
    ]
    paths = []
    def handle(request):
        paths.append(request.url.path)
        return httpx2.Response(200, json={"data": rows})
    class FixtureVault:
        def get(self, provider_id):
            return "public-fixture-token"
    provider = {"id": "example", "protocol": "openai", "baseUrl": "https://provider.example/v1",
                "manualModels": ["example/text", "owner/manual"]}
    models = await Catalog(FixtureVault(), transport=httpx2.MockTransport(handle)).models(provider)
    assert paths == ["/v1/models"]
    assert [model["id"] for model in models] == [row["id"] for row in rows] + ["owner/manual"]
    selected = next(model for model in models if model["id"] == "example/text")
    assert selected["capabilities"]["inputModalities"] == ["text", "image"]
    assert selected["capabilities"]["reasoningSupported"] is True
    assert selected["capabilities"]["reasoningEnumKnown"] is False
    batch = next(model for model in models if model["id"] == "example/text:batch")
    assert batch["capabilities"]["interactiveCompatible"] is False
    assert batch["capabilities"]["batchVariant"] is True
    assert next(model for model in models if model["id"] == "owner/manual")["capabilities"]["textChatCompatible"] is None
