"""Local output hygiene; no credential discovery or transcript scanning."""
from __future__ import annotations

import re

PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|password)\s*[:=]\s*)[^\s,\"']+"),
]


def sanitize(value, secrets=()):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        for pattern in PATTERNS:
            value = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", value)
        return value
    if isinstance(value, list):
        return [sanitize(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.lower() in {"apikey", "api_key", "secret", "password", "authorization"} else sanitize(item, secrets) for key, item in value.items()}
    return value


redact = sanitize


def safe_error(exc, secrets=()):
    return {"type": type(exc).__name__, "message": sanitize(str(exc), secrets), "status": getattr(exc, "status_code", None)}
