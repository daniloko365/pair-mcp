# Reproducible backend bundle. Only reviewed runtime libraries; no optional CLI
# installers, development tests, state, .env or credentials enter the artifact.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, copy_metadata

root = Path(SPECPATH).resolve().parent
hidden = ["pair_core.cli_worker"]
data = [(str(root / "vendor" / "pal"), "vendor/pal"), (str(root / "plugins"), "plugins"), (str(root / "build" / "release-info.json"), ".")]
for package in ["mcp", "mcp_types", "openai", "anthropic", "httpx2"]:
    hidden += collect_submodules(package, filter=lambda name: not (name.startswith("mcp.cli") or name == "mcp.server.fastmcp" or name.startswith("openai.cli")))
    data += collect_data_files(package)
for package in ["mcp", "mcp-types", "pydantic", "openai", "anthropic", "httpx2"]:
    data += copy_metadata(package)
a = Analysis([str(root / "scripts" / "frozen_main.py")], pathex=[str(root)], binaries=[], datas=data,
             hiddenimports=hidden, hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=["mcp.cli", "openai.cli", "pytest", "pip_audit"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="PairCore", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=True,
          codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="PairCore")
