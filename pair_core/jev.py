"""Opt-in TypeSafe Jev typed checks/ranking; never removes raw evidence.

Wire schema follows https://docs.typesafe.ai/api. HTTPX supplies transport;
there are no generated-text heuristics or implicit paid retries.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from urllib.parse import urlparse

import httpx2 as httpx
from .security import sanitize

from .providers import Catalog, RoutingError, configured_provider, safe_base_url, snapshot_config, _input_upper_bound, _decimal


class JevTools:
    def __init__(self, vault, config, *, transport=None):
        self.vault, self.config, self.transport = vault, config, transport

    async def _evaluate(self, state, questions):
        config = snapshot_config(self.config)
        settings = config.get("jev", {})
        if not settings.get("enabled"):
            raise RoutingError("jev_disabled_enable_in_connections")
        provider = configured_provider(config, settings.get("providerId"))
        is_openrouter = urlparse(provider.get("baseUrl", "")).hostname == "openrouter.ai"
        if provider.get("protocol") != "jev" and not (is_openrouter and provider.get("protocol") == "openai"):
            raise RoutingError("jev_protocol_required")
        key = self.vault.get(provider["id"])
        if not key:
            raise RoutingError("missing_key")
        base = safe_base_url(provider)
        if urlparse(base).hostname == "openrouter.ai":
            # Official typed Decisions API, not Chat Completions. A user's
            # existing OpenRouter key suffices; no TypeSafe account/proxy token.
            api_root = base[:-3] if base.endswith("/v1") else base
            if not api_root.endswith("/api"):
                raise RoutingError("jev_openrouter_base_must_be_api")
            endpoint = api_root + "/alpha/decisions"
        else:
            endpoint = base + ("/systemone" if base.endswith("/v1") else "/v1/systemone")
        controls = config.get("limits", {})
        reservation = 0.0
        price = None
        if controls.get("budgetUsd") is not None:
            models = await Catalog(self.vault, transport=self.transport).models(provider)
            card = next((m for m in models if m["id"] == settings.get("model", "jev-latest")), {})
            price = card.get("pricing")
            # Jev has no generated-output cap; permit finite budgets only if
            # explicit catalog metadata confirms input-only billing.
            if not price or price.get("reserveOutputUsdPerToken", price["outputUsdPerToken"]) != 0:
                raise RoutingError("budget_pricing_unknown")
            reservation = (_input_upper_bound([{"state": state, "questions": questions}])
                           * price.get("reserveInputUsdPerToken", price["inputUsdPerToken"]) + price.get("requestUsd", 0))
            if reservation > controls["budgetUsd"]:
                raise RoutingError("budget_exhausted")
        try:
            async with httpx.AsyncClient(transport=self.transport, follow_redirects=False, timeout=controls.get("timeSeconds"), trust_env=False) as client:
                response = await client.post(endpoint, headers={"Authorization": "Bearer " + key},
                    json={"state": state, "model": settings.get("model", "jev-latest"), "questions": questions})
                response.raise_for_status()
                # Invalid JSON numeric constants remain recorded as strings,
                # never NaN/Infinity values that poison billing or JSON storage.
                raw = response.json(parse_constant=lambda value: value)
        except Exception as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            raise RoutingError("jev_http_" + str(status) if status else "jev_outcome_unknown", reserved_usd=reservation) from None
        safe_raw = sanitize(raw, secrets=(key,))
        if not isinstance(safe_raw, dict):
            raise RoutingError("jev_invalid_response", partial={"rawResponse": safe_raw, "costUsd": None,
                "reservedUsd": reservation, "costStatus": "unknown"}, reserved_usd=reservation)
        usage = safe_raw.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        cost_value = usage.get("cost")
        reported_cost = _decimal(cost_value) if isinstance(cost_value, (int, float)) and not isinstance(cost_value, bool) else None
        cost = float(reported_cost) if reported_cost is not None else None
        input_tokens = usage.get("input_tokens")
        valid_tokens = isinstance(input_tokens, int) and not isinstance(input_tokens, bool) and input_tokens >= 0
        if cost is None and price and valid_tokens and safe_raw.get("model") == settings.get("model", "jev-latest"):
            cost = usage["input_tokens"] * price.get("reserveInputUsdPerToken", price["inputUsdPerToken"]) + price.get("requestUsd", 0)
        result = {"providerId": provider["id"], "model": safe_raw.get("model"), "answers": safe_raw.get("answers"),
                "usage": safe_raw.get("usage", {}), "rawResponse": safe_raw,
                "rawInput": copy.deepcopy(state), "rawInputSha256": hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                "costUsd": cost, "reservedUsd": reservation if cost is None else 0,
                "costBasis": "provider_reported" if reported_cost is not None else "conservative_catalog_estimate" if cost is not None and price and price.get("basis") == "conservative_catalog_tiers" else "catalog_estimate" if cost is not None else "unknown",
                "costStatus": "unknown" if cost is None else "known", "warnings": ["Jev classifications are advice, not proof; raw evidence is retained."]}
        if not isinstance(raw.get("model"), str) or not isinstance(raw.get("answers"), dict):
            raise RoutingError("jev_invalid_response", partial=result, reserved_usd=result["reservedUsd"])
        return result

    @staticmethod
    def _probability(value):
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1

    async def check(self, state, question, *, threshold=0.8):
        if not isinstance(question, str) or not question.strip() or not self._probability(threshold) or threshold <= 0.5:
            raise RoutingError("invalid_jev_check")
        result = await self._evaluate(state, {"check": {"type": "noul", "instructions": question}})
        answer = result["answers"].get("check", {})
        if not isinstance(answer, dict) or answer.get("type") != "noul" or not self._probability(answer.get("noul")):
            raise RoutingError("jev_invalid_answer", partial=result, reserved_usd=result["reservedUsd"])
        probability = answer["noul"]
        return {**result, "answer": answer, "uncertain": (1 - threshold) < probability < threshold,
                "decision": True if probability >= threshold else False if probability <= 1 - threshold else None}

    async def rank(self, state, candidates, *, instructions="Rank relevance to the task", threshold=0.8):
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 255 or not isinstance(instructions, str) or not instructions.strip() or not self._probability(threshold):
            raise RoutingError("invalid_jev_candidates")
        criteria = {}
        for candidate in candidates:
            if not isinstance(candidate, dict) or not isinstance(candidate.get("id"), str) or not candidate["id"].strip() or candidate["id"] in criteria:
                raise RoutingError("invalid_jev_candidates")
            criteria[candidate["id"]] = candidate.get("description")
        result = await self._evaluate(state, {"rank": {"type": "choice", "instructions": instructions, "criteria": criteria}})
        answer = result["answers"].get("rank", {})
        if not isinstance(answer, dict):
            raise RoutingError("jev_invalid_answer", partial=result, reserved_usd=result["reservedUsd"])
        probabilities = answer.get("probabilities", {})
        if answer.get("type") != "choice" or answer.get("choice") not in criteria or not self._probability(answer.get("confidence")) or not isinstance(probabilities, dict) or set(probabilities) != set(criteria) or not all(self._probability(v) for v in probabilities.values()) or abs(sum(probabilities.values()) - 1) > 0.001:
            raise RoutingError("jev_invalid_answer", partial=result, reserved_usd=result["reservedUsd"])
        ranked = sorted(({**copy.deepcopy(c), "probability": probabilities[c["id"]]} for c in candidates), key=lambda c: c["probability"], reverse=True)
        return {**result, "answer": answer, "ranked": ranked, "rawCandidates": copy.deepcopy(candidates),
                "mandatoryIds": [c["id"] for c in candidates if c.get("mandatory") or c.get("explicit")],
                "uncertain": answer["confidence"] < threshold, "selectionApplied": False}
