"""One-time local owner migration into OS Keychain; never displays a value.

Not part of the public application workflow. The application consumes only the
two explicitly authorized provider keys; admin password/domain are not used.
"""
from pathlib import Path
import argparse
import json

from pair_core.store import Store
from pair_core.vault import Vault


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--helper", type=Path, required=True)
    args = parser.parse_args()
    allowed = {"OPENROUTER_API_KEY": "openrouter", "CLODEX_API_KEY": "clodex"}
    values = {}
    for line in args.source.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() in allowed:
            values[allowed[name.strip()]] = value.strip()
    store = Store(args.state)
    vault = Vault(args.state, args.helper)
    cfg = store.config()
    urls = {"openrouter": "https://openrouter.ai/api/v1", "clodex": "https://clodex.xyz/v1"}
    existing = {p["id"] for p in cfg["providers"]}
    installed = []
    for pid, value in values.items():
        if not value or value.startswith(("вставь", "insert", "replace")):
            continue
        if not vault.has(pid):
            vault.set(pid, value)
        if pid not in existing:
            cfg["providers"].append({"id": pid, "name": "OpenRouter" if pid == "openrouter" else "Clodex", "protocol": "openai", "baseUrl": urls[pid], "enabled": True, "manualModels": [], "allowLocal": False})
        installed.append(pid)
    if installed:
        store.save_config(cfg)
    print(json.dumps({"keyReferencesPrepared": installed, "secretValuesPrinted": False, "oldSourceModified": False}))


if __name__ == "__main__":
    main()
