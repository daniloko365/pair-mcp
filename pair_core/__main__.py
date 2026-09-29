from __future__ import annotations

import argparse
from pathlib import Path


def default_state():
    return Path.home() / "Library" / "Application Support" / "Pair"


def main():
    import os
    os.umask(0o077)
    from .build_info import get_build_info
    # Bind this process before serving/waiting; an app replacement on disk is
    # never evidence that already-running code has become the new build.
    get_build_info()
    parser = argparse.ArgumentParser(prog="pair")
    parser.add_argument("command", choices=["app-server", "mcp", "doctor", "cli-worker", "check-release", "integrate-codex", "integrate-claude", "uninstall-codex", "uninstall-claude"])
    parser.add_argument("--state", type=Path, default=default_state())
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--job")
    args = parser.parse_args()
    if args.command == "app-server":
        from .service import serve
        serve(args.state, args.port)
    elif args.command == "mcp":
        from .mcp_server import run
        run(args.state)
    elif args.command == "cli-worker":
        if not args.job:
            parser.error("cli-worker requires --job")
        from .cli_worker import main as worker_main
        worker_main(["--state", str(args.state), "--job", args.job])
    elif args.command == "check-release":
        import json
        from .build_info import release_probe
        result = release_probe()
        print(json.dumps(result))
        raise SystemExit(0 if result["ok"] else 1)
    elif args.command.startswith(("integrate-", "uninstall-")):
        import json
        from .installer import install, uninstall
        operation, client = args.command.split("-", 1)
        print(json.dumps((install if operation == "integrate" else uninstall)(client, args.state)))
    else:
        import json
        from .build_info import get_build_info
        from .cli import discover_executable
        print(json.dumps({"codex": bool(discover_executable("codex")), "claude": bool(discover_executable("claude")), "state": str(args.state), "secretsPrinted": False, "build": get_build_info()}))


if __name__ == "__main__":
    main()
