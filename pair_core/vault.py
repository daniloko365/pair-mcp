"""OS keychain broker. Never a plaintext credential-file fallback."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


class VaultError(RuntimeError):
    pass


class Vault:
    def __init__(self, state_dir: Path, helper: Path | None = None):
        self.state_dir = Path(state_dir)
        root = Path(__file__).resolve().parent.parent
        candidates = [Path(sys.executable).parent / "pair-keychain", Path(sys.executable).parent.parent.parent / "MacOS" / "pair-keychain", root / "dist" / "Pair.app" / "Contents" / "MacOS" / "pair-keychain", root / "build" / "PairKeychain"]
        detected = next((p for p in candidates if p.is_file()), candidates[-1])
        self.helper = Path(helper or os.environ.get("PAIR_KEYCHAIN_HELPER", str(detected)))

    def _call(self, operation, account, secret=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", account):
            raise VaultError("Invalid credential reference")
        if not self.helper.is_file():
            raise VaultError("Secure OS keychain helper is unavailable; build the native app first")
        payload = {"operation": operation, "account": account}
        if secret is not None:
            if not isinstance(secret, str) or not secret or len(secret) > 8192 or "\x00" in secret:
                raise VaultError("Invalid API key")
            payload["secret"] = secret
        result = subprocess.run([str(self.helper)], input=json.dumps(payload), capture_output=True, text=True, timeout=15, check=False)
        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            raise VaultError("Secure OS keychain returned an invalid response") from None
        if result.returncode or not data.get("ok"):
            raise VaultError(f"Secure OS keychain operation failed (status {data.get('status', data.get('error', 'unknown'))})")
        return data

    def get(self, provider_id):
        return self._call("get", provider_id).get("secret")

    def set(self, provider_id, key):
        self._call("set", provider_id, key)

    def delete(self, provider_id):
        self._call("delete", provider_id)

    def has(self, provider_id):
        if not self.helper.is_file():
            return False
        return self._call("has", provider_id).get("present", False)
