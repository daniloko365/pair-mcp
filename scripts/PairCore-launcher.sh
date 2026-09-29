#!/bin/sh
set -eu
PAIR_BUNDLE_MACOS=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PAIR_KEYCHAIN_HELPER="$PAIR_BUNDLE_MACOS/pair-keychain"
exec "$PAIR_BUNDLE_MACOS/../Resources/Core/PairCore" "$@"
