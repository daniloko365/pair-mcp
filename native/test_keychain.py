"""Native helper tests. Only a disposable, deliberately public fixture is used."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import unittest
import uuid


HELPER = Path(os.environ.get("PAIR_KEYCHAIN_HELPER", Path(__file__).resolve().parents[1] / "dist/Pair.app/Contents/MacOS/pair-keychain"))


def invoke(request):
    result = subprocess.run([str(HELPER)], input=json.dumps(request), text=True, capture_output=True, timeout=10)
    if result.stderr:
        raise AssertionError("Helper wrote diagnostics to stderr")
    return result.returncode, json.loads(result.stdout)


class KeychainContractTests(unittest.TestCase):
    def test_invalid_account_and_operation_fail_closed(self):
        code, output = invoke({"operation": "get", "account": "../../anything"})
        self.assertNotEqual(code, 0)
        self.assertEqual(output, {"ok": False, "error": -50})
        code, output = invoke({"operation": "scan", "account": "pair-fixture"})
        self.assertNotEqual(code, 0)
        self.assertNotIn("secret", output)

    def test_missing_account_is_honest(self):
        account = "pair-test-missing-" + uuid.uuid4().hex
        self.assertEqual(invoke({"operation": "has", "account": account}), (0, {"ok": True, "present": False}))
        self.assertEqual(invoke({"operation": "get", "account": account}), (0, {"ok": True, "secret": None}))

    def test_set_update_get_delete_without_secret_echo(self):
        account = "pair-test-public-" + uuid.uuid4().hex
        try:
            for fixture in ["PAIR_PUBLIC_NONSECRET_FIXTURE_A", "PAIR_PUBLIC_NONSECRET_FIXTURE_B"]:
                self.assertEqual(invoke({"operation": "set", "account": account, "secret": fixture}), (0, {"ok": True}))
                self.assertEqual(invoke({"operation": "has", "account": account}), (0, {"ok": True, "present": True}))
                self.assertEqual(invoke({"operation": "get", "account": account}), (0, {"ok": True, "secret": fixture}))
        finally:
            self.assertEqual(invoke({"operation": "delete", "account": account}), (0, {"ok": True}))
        self.assertEqual(invoke({"operation": "has", "account": account}), (0, {"ok": True, "present": False}))

    def test_empty_secret_rejected(self):
        code, output = invoke({"operation": "set", "account": "pair-test-empty", "secret": ""})
        self.assertNotEqual(code, 0)
        self.assertNotIn("secret", output)


if __name__ == "__main__":
    unittest.main()
