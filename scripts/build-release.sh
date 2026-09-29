#!/bin/sh
set -eu
PAIR_PRODUCT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PAIR_BUNDLE_OUTPUT=${PAIR_BUNDLE_OUTPUT:-"$PAIR_PRODUCT_ROOT/dist/Pair.app"}
cd "$PAIR_PRODUCT_ROOT"
uv sync --frozen
uv run python scripts/release-info.py --output build/release-info.json --legal-output build/Legal
uv run pyinstaller --noconfirm --distpath build/frozen --workpath build/pyinstaller scripts/PairCore.spec
if [ -d "$PAIR_BUNDLE_OUTPUT" ]; then
  PAIR_PREVIOUS_BUNDLE=$(mktemp -d "$PAIR_PRODUCT_ROOT/build/bundle-previous.XXXXXX")
  mv "$PAIR_BUNDLE_OUTPUT" "$PAIR_PREVIOUS_BUNDLE/Pair.app"
fi
sh native/build-native.sh "$PAIR_BUNDLE_OUTPUT"
mkdir -p "$PAIR_BUNDLE_OUTPUT/Contents/Resources/Core"
cp -R build/frozen/PairCore/. "$PAIR_BUNDLE_OUTPUT/Contents/Resources/Core/"
clang -Os -mmacosx-version-min=13.0 scripts/PairCore-launcher.c -o "$PAIR_BUNDLE_OUTPUT/Contents/MacOS/PairCore"
mkdir -p "$PAIR_BUNDLE_OUTPUT/Contents/Resources/Legal"
cp -R build/Legal/. "$PAIR_BUNDLE_OUTPUT/Contents/Resources/Legal/"
cp LICENSE "$PAIR_BUNDLE_OUTPUT/Contents/Resources/Legal/PAIR-LICENSE"
cp vendor/pal/LICENSE "$PAIR_BUNDLE_OUTPUT/Contents/Resources/Legal/PAL-LICENSE"
cp vendor/pal/NOTICE "$PAIR_BUNDLE_OUTPUT/Contents/Resources/Legal/PAL-NOTICE"
cp THIRD_PARTY_PAL.md "$PAIR_BUNDLE_OUTPUT/Contents/Resources/Legal/"
codesign --force --sign - "$PAIR_BUNDLE_OUTPUT/Contents/Resources/Core/PairCore"
codesign --force --sign - "$PAIR_BUNDLE_OUTPUT/Contents/MacOS/PairCore"
codesign --force --sign - "$PAIR_BUNDLE_OUTPUT"
codesign --verify --deep --strict "$PAIR_BUNDLE_OUTPUT"
printf '%s\n' "$PAIR_BUNDLE_OUTPUT"
