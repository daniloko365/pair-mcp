#!/bin/sh
set -eu
PRODUCT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUTPUT=${1:-"$PRODUCT_ROOT/dist/Pair.app"}
ARCH=$(uname -m)
mkdir -p "$OUTPUT/Contents/MacOS" "$OUTPUT/Contents/Resources/product"
swiftc -swift-version 5 -target "$ARCH-apple-macosx13.0" -O -framework Security "$PRODUCT_ROOT/native/KeychainHelper.swift" -o "$OUTPUT/Contents/MacOS/pair-keychain"
swiftc -swift-version 5 -target "$ARCH-apple-macosx13.0" -parse-as-library -O -framework AppKit -framework SwiftUI "$PRODUCT_ROOT/native/PairApp.swift" -o "$OUTPUT/Contents/MacOS/Pair"
cp "$PRODUCT_ROOT/native/Info.plist" "$OUTPUT/Contents/Info.plist"
# Runtime packaging is completed by the product release builder. This source
# copy also permits a self-contained app to find its backend without cwd magic.
cp -R "$PRODUCT_ROOT/pair_core" "$OUTPUT/Contents/Resources/product/"
if [ -x "$PRODUCT_ROOT/.venv/bin/python" ]; then
  printf '%s\n' "$PRODUCT_ROOT/.venv/bin/python" > "$OUTPUT/Contents/Resources/python-path.txt"
fi
codesign --force --sign - "$OUTPUT/Contents/MacOS/pair-keychain"
codesign --force --sign - "$OUTPUT"
printf '%s\n' "$OUTPUT"
