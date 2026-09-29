"""Small per-provider routing policy over the official SDK transports/parsers.

No credentials are accepted from MCP input, no implicit provider selection, and
SDK retries are disabled. Pricing is provider-supplied metadata, not a guarantee
of invoice totals. Unknown outcomes retain their preflight reservation.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import hashlib
import ipaddress
import json
import math
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from urllib.parse import urlparse

import anthropic
import httpx2 as httpx
import openai

from .security import sanitize

CLODEX_TTFT_SECONDS = 15.0


class RoutingError(Exception):
    """Safe public error; never includes SDK exception text or response bodies."""
    def __init__(self, code, *, attempts=None, partial=None, reserved_usd=0, status_code=None):
        super().__init__(code)
        self.code = code
        self.attempts = attempts or []
        self.partial = partial
        self.reserved_usd = reserved_usd
        self.status_code = status_code

    def public(self):
        partial_cost = (self.partial or {}).get("costUsd")
        settled = (self.partial or {}).get("costStatus") == "known" and self.reserved_usd == 0
        return {"code": self.code, "attempts": self.attempts, "partial": self.partial,
                "costUsd": partial_cost if settled else None, "knownCostUsd": partial_cost or 0,
                "reservedUsd": self.reserved_usd, "costStatus": "known" if settled else "unknown"}


def snapshot_config(config):
    return copy.deepcopy(config() if callable(config) else config)


def configured_provider(config, provider_id):
    match = next((p for p in config.get("providers", []) if p["id"] == provider_id), None)
    if not match or not match.get("enabled", True):
        raise RoutingError("provider_unavailable")
    return match


def safe_base_url(provider):
    """Only settings-originated endpoints; never URLs from routes/prompts."""
    url = provider.get("baseUrl", "")
    parsed = urlparse(url)
    local = bool(provider.get("allowLocal", False))
    if parsed.scheme not in (("http", "https") if local else ("https",)):
        raise RoutingError("invalid_endpoint")
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RoutingError("invalid_endpoint")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if not loopback:
            raise RoutingError("http_requires_loopback")
    try:
        if not ipaddress.ip_address(parsed.hostname).is_global and not local:
            raise RoutingError("private_endpoint_requires_confirmation")
    except ValueError:
        if not local and (parsed.hostname == "localhost" or parsed.hostname.endswith((".local", ".localhost"))):
            raise RoutingError("private_endpoint_requires_confirmation")
    return url.rstrip("/")


def validate_routes(routes):
    if not isinstance(routes, list) or not routes:
        raise RoutingError("routes_required")
    for route in routes:
        if not isinstance(route, dict) or set(route) - {"providerId", "model", "effort"}:
            raise RoutingError("route_fields")
        if not all(isinstance(route.get(k), str) and route[k].strip() for k in ("providerId", "model")):
            raise RoutingError("invalid_route")
        if any(ord(c) < 32 for c in route["model"]):
            raise RoutingError("invalid_model")
        if route.get("effort") not in {None, "none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise RoutingError("invalid_effort")


def validate_limits(limits):
    if not isinstance(limits, dict) or set(limits) - {"maxTokens", "timeSeconds", "budgetUsd"}:
        raise RoutingError("invalid_limits")
    for name in ("maxTokens", "timeSeconds", "budgetUsd"):
        value = limits.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise RoutingError("invalid_limits")
        if value < 0 or (name != "budgetUsd" and value <= 0):
            raise RoutingError("invalid_limits")
        if name == "maxTokens" and not isinstance(value, int):
            raise RoutingError("invalid_limits")


def _decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _pricing(raw):
    values = raw.get("pricing") or {}
    if not isinstance(values, dict):
        return None
    source = {}
    for normalized, upstream in (("inputUsdPerToken", "prompt"), ("outputUsdPerToken", "completion")):
        value = _decimal(values.get(normalized, values.get(upstream)))
        if value is None:
            return None
        source[normalized] = float(value)
    # Profile: text messages, no tools/plugins/media/cache-control. Only known
    # unused add-ons are excluded. Unknown positive charges still fail closed.
    # Units/overrides: OpenRouterTeam/terraform-provider-openrouter model schema.
    input_keys = {"prompt", "inputUsdPerToken", "input_cache_read", "input_cache_write",
                  "input_cache_write_1h", "input_cache_write_5m", "cache_write_1h", "cache_write_5m"}
    output_keys = {"completion", "outputUsdPerToken"}
    optional_unused = {"web_search", "image", "image_output", "image_token", "audio", "audio_output", "input_audio_cache"}
    conditions = {"min_prompt_tokens", "utc_start", "utc_end", "utc_days"}
    input_rates, output_rates = [], []
    reasoning_rates, request_rates = [Decimal(0)], [Decimal(0)]
    overrides = values.get("overrides", [])
    if not isinstance(overrides, list) or any(not isinstance(tier, dict) for tier in overrides):
        return None
    for tier in [values, *overrides]:
        for name, value in tier.items():
            if name == "overrides" or name in conditions:
                continue
            number = _decimal(value)
            if name == "discount":
                # Ignoring a verified discount is conservative, not a fee.
                if number is None or number > 1:
                    return None
            elif name in input_keys:
                if number is None:
                    return None
                input_rates.append(number)
            elif name in output_keys:
                if number is None:
                    return None
                output_rates.append(number)
            elif name == "internal_reasoning":
                if number is None:
                    return None
                reasoning_rates.append(number)
            elif name in {"request", "requestUsd"}:
                if number is None:
                    return None
                request_rates.append(number)
            elif name in optional_unused:
                if number is None:
                    return None
            elif number != 0:
                return None
    # Across every conditional tier, including cache-write rates. Do not infer
    # which tier/discount an opaque upstream will actually select.
    source.update(reserveInputUsdPerToken=float(max(input_rates)),
                  reserveOutputUsdPerToken=float(max(output_rates) + max(reasoning_rates)),
                  requestUsd=float(max(request_rates)))
    source["basis"] = "conservative_catalog_tiers" if (overrides or source["reserveInputUsdPerToken"] != source["inputUsdPerToken"]
        or source["reserveOutputUsdPerToken"] != source["outputUsdPerToken"] or source["requestUsd"] > 0) else "catalog_base"
    return source


def normalize_model(raw, *, source="live"):
    mid = raw.get("id") or raw.get("name")
    if not isinstance(mid, str) or not mid:
        return None
    raw_parameters = raw.get("supported_parameters")
    parameters_known = isinstance(raw_parameters, list) and all(isinstance(v, str) for v in raw_parameters)
    parameters = raw_parameters if parameters_known else []
    advertised_reasoning = raw.get("reasoning")
    effort_values = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
    explicit_enum = isinstance(advertised_reasoning, list) and all(isinstance(v, str) and v in effort_values for v in advertised_reasoning)
    reasoning = advertised_reasoning if explicit_enum else []
    reasoning_known = isinstance(advertised_reasoning, bool) or (
        isinstance(advertised_reasoning, list) and all(isinstance(v, str) for v in advertised_reasoning))
    supported_reasoning = (reasoning_known and bool(advertised_reasoning)) or any(
        x in parameters for x in ("reasoning", "reasoning_effort"))
    architecture = raw.get("architecture")
    architecture = architecture if isinstance(architecture, dict) else {}
    def modalities(name):
        values = architecture.get(name)
        return list(values) if isinstance(values, list) and values and all(
            isinstance(v, str) and v for v in values) else None
    input_modalities, output_modalities = modalities("input_modalities"), modalities("output_modalities")
    # Missing catalog metadata is unknown, not a claim that text chat is
    # supported or forbidden. Only explicit modalities establish a verdict.
    text_chat = None
    if any(values is not None and "text" not in values for values in (input_modalities, output_modalities)):
        text_chat = False
    elif input_modalities is not None and output_modalities is not None:
        text_chat = True
    # OpenRouter documents :batch as an asynchronous endpoint variant. Keep
    # this ID hint separate from modalities and the legacy generation guard;
    # a custom/manual endpoint's actual contract still belongs to its owner.
    batch_variant = mid.lower().endswith(":batch")
    interactive = False if batch_variant else text_chat
    compatibility_reason = ("batch_variant" if batch_variant else "non_text_modalities" if text_chat is False
                            else "text_chat_advertised" if text_chat is True else "metadata_unknown")
    result = {"id": mid, "name": raw.get("display_name") or raw.get("name") or mid,
              "source": source, "reasoning": reasoning,
              "capabilities": {"inputModalities": input_modalities, "outputModalities": output_modalities,
                               "textChatCompatible": text_chat,
                               "batchVariant": batch_variant, "interactiveCompatible": interactive,
                               "compatibilityReason": compatibility_reason,
                               # Legacy routing stays permissive for manual/unknown IDs.
                               "chat": text_chat is not False,
                               "tools": "tools" in parameters,
                               "reasoningSupported": supported_reasoning,
                               "reasoningEnumKnown": explicit_enum,
                               "reasoningKnown": parameters_known or reasoning_known}}
    price = _pricing(raw)
    if price is not None:
        result["pricing"] = price
    context = raw.get("contextLength", raw.get("context_length"))
    if isinstance(context, int) and context > 0:
        result["contextLength"] = context
    maximum = (raw.get("top_provider") or {}).get("max_completion_tokens", raw.get("max_output_tokens"))
    if isinstance(maximum, int) and maximum > 0:
        result["maxOutputTokens"] = maximum
    return result


class Catalog:
    def __init__(self, vault, *, transport=None, ttl_seconds=60, clock=None):
        self.vault, self.transport = vault, transport
        self.ttl_seconds = ttl_seconds
        self.clock = clock or time.monotonic
        self._cache = {}
        self._locks = {}
        self.status = {}

    async def models(self, provider):
        base = safe_base_url(provider)
        key = self.vault.get(provider["id"])
        if not key:
            raise RoutingError("missing_key")
        signature = (base, provider.get("protocol", "openai"), tuple(provider.get("manualModels", [])),
                     provider.get("enabled", True), provider.get("allowLocal", False),
                     hashlib.sha256(key.encode()).digest())
        pid = provider["id"]
        async with self._locks.setdefault(pid, asyncio.Lock()):
            cached = self._cache.get(pid)
            hit = cached is not None and cached["signature"] == signature and self.clock() - cached["at"] < self.ttl_seconds
            if not hit:
                rows = await self._fetch_models(provider, base, key)
                cached = {"signature": signature, "at": self.clock(), "syncedAt": time.time(), "rows": copy.deepcopy(rows)}
                self._cache[pid] = cached
            self.status[pid] = {"cached": hit, "syncedAt": cached["syncedAt"]}
            result = copy.deepcopy(cached["rows"])
            for model in result:
                model["catalog"] = dict(self.status[pid])
            return result

    async def _fetch_models(self, provider, base, key):
        protocol = provider.get("protocol", "openai")
        try:
            async with httpx.AsyncClient(transport=self.transport, follow_redirects=False, timeout=20, trust_env=False) as http:
                if protocol == "anthropic":
                    sdk_base = base[:-3] if base.endswith("/v1") else base
                    async with anthropic.AsyncAnthropic(api_key=key, base_url=sdk_base, max_retries=0, http_client=http) as sdk:
                        page = await sdk.models.list(limit=1000)
                        data = [m.model_dump() async for m in page]
                elif protocol == "openai":
                    async with openai.AsyncOpenAI(api_key=key, base_url=base, max_retries=0, http_client=http) as sdk:
                        # OpenRouter defaults its models API to text output.
                        # Preserve the complete catalog for the explicit All
                        # view; don't send this vendor query to custom APIs.
                        query = {"output_modalities": "all"} if urlparse(base).hostname == "openrouter.ai" else None
                        page = await sdk.models.list(extra_query=query) if query else await sdk.models.list()
                        data = [m.model_dump() async for m in page]
                elif protocol == "jev":
                    endpoint = base + ("/models" if base.endswith("/v1") else "/v1/models")
                    response = await http.get(endpoint, headers={"Authorization": "Bearer " + key})
                    response.raise_for_status()
                    body = response.json()
                    data = body.get("data", body.get("models", [])) if isinstance(body, dict) else body
                else:
                    raise RoutingError("unsupported_protocol")
        except RoutingError:
            raise
        except Exception as exc:
            # Manual IDs remain useful if a compatible service has no catalog.
            status = getattr(exc, "status_code", None)
            if status is None and isinstance(exc, httpx.HTTPStatusError):
                status = exc.response.status_code
            if status in (404, 405) and provider.get("manualModels"):
                data = []
            else:
                if status:
                    code = "catalog_http_" + str(status)
                elif isinstance(exc, (httpx.TimeoutException, openai.APITimeoutError, anthropic.APITimeoutError, TimeoutError)):
                    code = "catalog_timeout"
                elif isinstance(exc, (httpx.TransportError, openai.APIConnectionError, anthropic.APIConnectionError, ConnectionError)):
                    code = "catalog_connection_error"
                else:
                    code = "catalog_unavailable"
                raise RoutingError(code) from None
        normalized = {}
        for raw in data:
            if isinstance(raw, dict) and (model := normalize_model(raw)):
                normalized[model["id"]] = model
        for mid in provider.get("manualModels", []):
            if mid not in normalized:
                normalized[mid] = normalize_model({"id": mid}, source="manual")
        return sanitize(list(normalized.values()), secrets=(key,))


def _input_upper_bound(messages):
    # Byte-count, plus message framing, intentionally overestimates ordinary
    # text tokens. No tokenizer/pricing identity is guessed from a display name.
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 64 * len(messages)


def _budget_plan(metadata, messages, limits, remaining, warnings):
    max_tokens = limits.get("maxTokens")
    price = metadata.get("pricing") if metadata else None
    if remaining is not None and price is None:
        raise RoutingError("budget_pricing_unknown")
    if price:
        input_price = Decimal(str(price.get("reserveInputUsdPerToken", price["inputUsdPerToken"])))
        output_price = Decimal(str(price.get("reserveOutputUsdPerToken", price["outputUsdPerToken"])))
        input_reserve = Decimal(_input_upper_bound(messages)) * input_price + Decimal(str(price.get("requestUsd", 0)))
        if price.get("basis") == "conservative_catalog_tiers":
            warnings.append("Conservative catalog estimate uses maximum token/cache/conditional-tier rates and mandatory request fees; unused search/media add-ons excluded. Not an invoice.")
        if remaining is not None:
            available = Decimal(str(remaining)) - input_reserve
            if available < 0:
                raise RoutingError("budget_exhausted")
            if output_price:
                affordable = int((available / output_price).to_integral_value(rounding=ROUND_FLOOR))
                if affordable < 1:
                    raise RoutingError("budget_exhausted")
                if max_tokens is None or max_tokens > affordable:
                    max_tokens = affordable
                    warnings.append(f"budget-derived output token cap: {affordable}")
        if max_tokens is None:
            max_tokens = metadata.get("maxOutputTokens")
        elif metadata.get("maxOutputTokens") and max_tokens > metadata["maxOutputTokens"]:
            max_tokens = metadata["maxOutputTokens"]
            warnings.append(f"Provider-advertised output token maximum: {max_tokens}")
        reserve = input_reserve + output_price * (max_tokens or 0)
        return max_tokens, float(reserve), price
    return max_tokens, 0.0, None


async def _notify(callback, data):
    if callback:
        result = callback(data)
        if inspect.isawaitable(result):
            await result


class ProviderRouter:
    def __init__(self, vault, config, *, transport=None, catalog=None):
        self.vault, self.config, self.transport = vault, config, transport
        self.catalog = catalog or Catalog(vault, transport=transport)

    async def complete(self, routes, messages, limits, job_id=None, on_event=None):
        validate_routes(routes)
        validate_limits(limits)
        if not isinstance(messages, list) or not messages or any(
            not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant")
            or not isinstance(m.get("content"), str) for m in messages):
            raise RoutingError("text_messages_required")
        frozen = snapshot_config(self.config)
        # Check every route before sending anything: invalid fallback config must
        # not become a late failure after a charge on the preferred source.
        for route in routes:
            safe_base_url(configured_provider(frozen, route["providerId"]))
        ledger = {"attempts": [], "reserved": 0.0, "activeAllocation": 0.0, "state": None, "route": None, "key": ""}
        try:
            async with asyncio.timeout(limits.get("timeSeconds")):
                return await self._complete(frozen, copy.deepcopy(routes), copy.deepcopy(messages),
                                            copy.deepcopy(limits), on_event, ledger)
        except TimeoutError:
            reserve = ledger["reserved"] + ledger["activeAllocation"]
            partial = None
            if ledger["state"] and ledger["state"]["text"]:
                partial = self._result(ledger["route"], ledger["state"], ledger["attempts"],
                                       ["User time limit expired; no automatic retry."], None,
                                       reserve, True, ledger["key"])
            await _notify(on_event, {"type": "attempt_error", "attemptId": (ledger.get("attempt") or {}).get("attemptId"),
                                     "status": "timed_out", "reservedUsd": ledger["activeAllocation"],
                                     "costUsd": None, "costStatus": "unknown", "partial": partial})
            raise RoutingError("time_limit", attempts=ledger["attempts"], partial=partial,
                               reserved_usd=reserve) from None
        except asyncio.CancelledError:
            partial = None
            if ledger["state"] and ledger["state"]["text"]:
                partial = self._result(ledger["route"], ledger["state"], ledger["attempts"], [], None,
                                       ledger["activeAllocation"], True, ledger["key"])
            await _notify(on_event, {"type": "attempt_cancelled", "attemptId": (ledger.get("attempt") or {}).get("attemptId"),
                                     "status": "cancelled", "reservedUsd": ledger["activeAllocation"],
                                     "costUsd": None, "costStatus": "unknown", "partial": partial})
            raise

    async def _complete(self, config, routes, messages, limits, on_event, ledger):
        attempts, warnings = ledger["attempts"], []
        reserved = 0.0
        unknown_charge = False
        for route in routes:
            provider = configured_provider(config, route["providerId"])
            key = self.vault.get(provider["id"])
            if not key:
                raise RoutingError("missing_key", attempts=attempts, reserved_usd=reserved)
            if provider.get("protocol") == "jev":
                raise RoutingError("jev_is_not_text_generation")
            catalog_began = time.monotonic()
            try:
                catalog = await self.catalog.models(provider)
                metadata = next((m for m in catalog if m["id"] == route["model"]), None)
            except RoutingError as exc:
                metadata = None
                warnings.append("catalog metadata unavailable: " + exc.code)
            await _notify(on_event, {"type": "catalog_preflight", "providerId": provider["id"], "selectedModel": route["model"],
                                    "durationSeconds": round(time.monotonic() - catalog_began, 4),
                                    **getattr(self.catalog, "status", {}).get(provider["id"], {}),
                                    "status": "ready" if metadata else "metadata_unknown"})
            if metadata and not metadata["capabilities"].get("chat", True):
                raise RoutingError("model_not_chat_compatible", attempts=attempts, reserved_usd=reserved)
            remaining = None if limits.get("budgetUsd") is None else max(0.0, limits["budgetUsd"] - reserved)
            try:
                max_tokens, allocation, pricing = _budget_plan(metadata, messages, limits, remaining, warnings)
            except RoutingError as exc:
                exc.attempts, exc.reserved_usd = attempts, reserved
                raise
            attempt = {"attemptId": str(uuid.uuid4()), "providerId": route["providerId"], "selectedModel": route["model"], "status": "running"}
            attempts.append(attempt)
            state = {"text": "", "model": None, "usage": {}, "reportedCost": None, "useful": False}
            ledger.update(reserved=reserved, activeAllocation=allocation, state=state, route=route, key=key, attempt=attempt)
            await _notify(on_event, {"type": "attempt_start", **attempt, "reservedUsd": allocation,
                                     "costUsd": None, "costStatus": "pending"})
            try:
                await self._stream(provider, route, key, messages, max_tokens, metadata, state, warnings, on_event)
                if not state["text"].strip():
                    raise RoutingError("empty_response")
            except (Exception, asyncio.CancelledError) as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                status = getattr(exc, "status_code", None)
                sdk_code = getattr(exc, "code", None)
                if status is None and str(sdk_code).isdigit() and 400 <= int(sdk_code) <= 599:
                    # Official OpenAI SSE parser raises APIError with .code,
                    # rather than HTTPStatusError, for an in-stream error event.
                    status = int(sdk_code)
                timeout = isinstance(exc, (TimeoutError, openai.APITimeoutError, anthropic.APITimeoutError))
                code = exc.code if isinstance(exc, RoutingError) else (
                    "first_output_timeout" if timeout else "http_" + str(status) if status else "upstream_interrupted")
                attempt.update(status="failed", code=code, usefulOutput=state["useful"])
                attempt_reserve = 0.0
                # A terminal 4xx can only imply no charge before the stream
                # supplied any output or usage. An in-stream 429 after tokens
                # is not evidence that the provider billed zero.
                stream_started = bool(state["useful"] or state["text"] or state["usage"] or
                                      state["model"] or state["reportedCost"] is not None)
                if stream_started or status not in (400, 401, 403, 404, 405, 422, 429):
                    reserved += allocation
                    attempt_reserve = allocation
                    unknown_charge = True
                ledger.update(reserved=reserved, activeAllocation=0.0)
                partial = self._result(route, state, attempts, warnings, pricing, reserved, unknown_charge, key) if state["text"].strip() else None
                await _notify(on_event, {"type": "attempt_error", **attempt, "reservedUsd": attempt_reserve,
                                         "costUsd": None if unknown_charge else 0, "costStatus": "unknown" if unknown_charge else "known",
                                         "partial": partial})
                retryable = status in (429, 499) or (status is not None and 500 <= status <= 599) or code in ("first_output_timeout", "empty_response")
                if state["useful"] or not retryable or route is routes[-1]:
                    raise RoutingError(code, attempts=attempts, partial=partial, reserved_usd=reserved) from None
                continue
            result = self._result(route, state, attempts, warnings, pricing, reserved, unknown_charge, key)
            attempt.update(status="completed", actualModel=state["model"])
            if result["costUsd"] is None:
                result["reservedUsd"] += allocation
            await _notify(on_event, {"type": "attempt_complete", **attempt,
                                     "reservedUsd": allocation if result["costUsd"] is None else 0,
                                     "costUsd": result["costUsd"], "costStatus": result["costStatus"]})
            ledger.update(activeAllocation=0.0)
            return result
        raise RoutingError("no_route_completed", attempts=attempts, reserved_usd=reserved)

    @staticmethod
    def _result(route, state, attempts, warnings, pricing, reserved, unknown_charge, key):
        cost = state["reportedCost"]
        usage = state["usage"]
        reported = cost is not None
        complete_usage = all(isinstance(usage.get(k), int) and not isinstance(usage[k], bool) and usage[k] >= 0 for k in ("inputTokens", "outputTokens"))
        if cost is None and pricing and state["model"] == route["model"] and complete_usage:
            cost = (usage["inputTokens"] * pricing.get("reserveInputUsdPerToken", pricing["inputUsdPerToken"])
                    + usage["outputTokens"] * pricing.get("reserveOutputUsdPerToken", pricing["outputUsdPerToken"])
                    + pricing.get("requestUsd", 0))
            warnings = warnings + ["Cost estimated from this provider's catalog and token usage; not a reconciled invoice."]
        elif cost is None and pricing and state["model"] != route["model"]:
            warnings = warnings + ["Actual model differs from the priced selected model; charge remains unknown."]
        if unknown_charge:
            warnings = warnings + ["An earlier attempt has unknown charges; reservation retained. No same-route retry."]
        if cost is None:
            warnings = warnings + ["Provider charge unknown, not zero. Reconcile before retrying this job."]
        actual = state["model"]
        if actual is None:
            warnings = warnings + ["Upstream did not report its model identity."]
        return {"text": state["text"].replace(key, "[REDACTED]"), "providerId": route["providerId"],
                "model": actual, "selectedModel": route["model"], "usage": usage,
                "costUsd": cost, "reservedUsd": reserved, "costStatus": "known" if cost is not None and not unknown_charge else "unknown",
                "costBasis": "provider_reported" if reported else "conservative_catalog_estimate" if cost is not None and pricing and pricing.get("basis") == "conservative_catalog_tiers" else "catalog_estimate" if cost is not None else "unknown",
                "attempts": attempts, "warnings": warnings}

    async def _stream(self, provider, route, key, messages, max_tokens, metadata, state, warnings, callback):
        clodex = "clodex" in (provider.get("name", "") + " " + provider["baseUrl"]).lower()
        first_deadline = time.monotonic() + CLODEX_TTFT_SECONDS if clodex else None
        async def wait(awaitable):
            if first_deadline is not None and not state["useful"]:
                return await asyncio.wait_for(awaitable, max(0.0001, first_deadline - time.monotonic()))
            return await awaitable
        effort = route.get("effort")
        supported = metadata and metadata["capabilities"].get("reasoningKnown")
        if effort and supported and not metadata["capabilities"].get("reasoningSupported"):
            warnings.append("Reasoning is not advertised by this model; requested effort omitted.")
            effort = None
        elif effort and supported and metadata["capabilities"].get("reasoningEnumKnown") and effort not in metadata.get("reasoning", []):
            warnings.append("Selected reasoning effort is not advertised by this model; omitted.")
            effort = None
        elif effort and supported and not metadata["capabilities"].get("reasoningEnumKnown"):
            warnings.append("Reasoning effort enum is unknown; forwarding user-selected effort for upstream validation.")
        elif effort and not supported:
            warnings.append("Reasoning support is unknown for this model; upstream must validate the requested effort.")
        protocol = provider.get("protocol", "openai")
        async with httpx.AsyncClient(transport=self.transport, timeout=None, follow_redirects=False, trust_env=False) as http:
            if protocol == "openai":
                async with openai.AsyncOpenAI(api_key=key, base_url=safe_base_url(provider), max_retries=0,
                                              timeout=None, http_client=http) as sdk:
                    kwargs = {"model": route["model"], "messages": messages, "stream": True,
                              "stream_options": {"include_usage": True}}
                    if max_tokens is not None:
                        kwargs["max_completion_tokens"] = max_tokens
                    if effort:
                        if "openrouter.ai" in provider["baseUrl"]:
                            kwargs["extra_body"] = {"reasoning": {"effort": effort}}
                        else:
                            kwargs["reasoning_effort"] = effort
                    stream = await wait(sdk.chat.completions.create(**kwargs))
                    async with stream:
                        iterator = stream.__aiter__()
                        while True:
                            try:
                                chunk = await wait(iterator.__anext__())
                            except StopAsyncIteration:
                                break
                            if chunk.model:
                                state["model"] = chunk.model.replace(key, "[REDACTED]")
                            wire = chunk.model_dump()
                            if isinstance(wire.get("error"), dict):
                                code = wire["error"].get("code")
                                status = int(code) if isinstance(code, (str, int)) and str(code).isdigit() else None
                                raise RoutingError("http_" + str(status) if status else "upstream_stream_error", status_code=status)
                            if chunk.usage:
                                raw = chunk.usage.model_dump()
                                state["usage"] = {name: raw[field] for name, field in (("inputTokens", "prompt_tokens"), ("outputTokens", "completion_tokens"), ("totalTokens", "total_tokens"))
                                                  if isinstance(raw.get(field), int) and not isinstance(raw[field], bool) and raw[field] >= 0}
                                reported = _decimal(raw.get("cost"))
                                if reported is not None:
                                    state["reportedCost"] = float(reported)
                            for choice in chunk.choices or []:
                                delta = choice.delta
                                raw = delta.model_dump()
                                # Reasoning is useful output for TTFT but is not
                                # confused with the final opinion text.
                                useful = bool((delta.content or "").strip()) or any(
                                    bool(value.strip()) if isinstance(value, str) else bool(value)
                                    for value in (raw.get("reasoning"), raw.get("reasoning_content"), raw.get("reasoning_details"), delta.tool_calls))
                                if useful:
                                    state["useful"] = True
                                if delta.content:
                                    state["text"] += delta.content
                                    # No raw token callbacks: a credential split
                                    # across deltas defeats per-delta redaction.
                                    # Only the assembled, redacted result escapes.
            elif protocol == "anthropic":
                base = safe_base_url(provider)
                sdk_base = base[:-3] if base.endswith("/v1") else base
                async with anthropic.AsyncAnthropic(api_key=key, base_url=sdk_base, max_retries=0,
                                                    timeout=None, http_client=http) as sdk:
                    if max_tokens is None:
                        # Native Messages requires this parameter: do not invent
                        # an invisible 1K/4K cap. Use advertised model maximum.
                        max_tokens = (metadata or {}).get("maxOutputTokens")
                        if max_tokens is None:
                            raise RoutingError("anthropic_output_limit_required")
                    kwargs = {"model": route["model"], "max_tokens": max_tokens, "stream": True,
                              "messages": [m for m in messages if m["role"] != "system"]}
                    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
                    if system:
                        kwargs["system"] = system
                    if effort:
                        kwargs.update(thinking={"type": "adaptive"}, output_config={"effort": effort})
                    stream = await wait(sdk.messages.create(**kwargs))
                    async with stream:
                        iterator = stream.__aiter__()
                        while True:
                            try:
                                event = await wait(iterator.__anext__())
                            except StopAsyncIteration:
                                break
                            raw = event.model_dump()
                            if event.type == "message_start":
                                state["model"] = event.message.model.replace(key, "[REDACTED]")
                                state["usage"] = {"inputTokens": event.message.usage.input_tokens,
                                                  "outputTokens": event.message.usage.output_tokens}
                            elif event.type == "message_delta":
                                state["usage"]["outputTokens"] = event.usage.output_tokens
                            elif event.type == "content_block_delta":
                                delta = raw.get("delta", {})
                                if any(isinstance(delta.get(field), str) and delta[field].strip()
                                       for field in ("text", "thinking", "partial_json")):
                                    state["useful"] = True
                                if delta.get("text"):
                                    state["text"] += delta["text"]
                            elif event.type == "error":
                                raise RoutingError("upstream_stream_error")
            else:
                raise RoutingError("unsupported_protocol")
