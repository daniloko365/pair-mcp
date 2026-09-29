# Native Mac panel

`sh native/build-native.sh` builds `dist/Pair.app` for the current Mac architecture,
with a macOS 13 deployment target. The release builder supplies the standalone
`Contents/MacOS/PairCore` backend; development builds use the product's `.venv` through
a path recorded inside the app. `PAIR_PYTHON` and `PAIR_ROOT` are development overrides, not provider
credentials. `PAIR_STATE` selects an isolated test state directory.

The menu-bar icon opens or hides the native window with one left click. The three
tabs are Council, Pair and Connections (Russian labels). Closing the window leaves
the menu-bar app running; right-click → Quit closes only the panel, not shared backend jobs. Cmd-S saves
dirty settings. Existing worker jobs keep their configuration snapshot.

Provider keys are entered into a secure field and sent once to the private loopback
API. The UI only receives `keyPresent`, never a saved key. Pending unsaved keys stay
in process memory. App/server handshake tokens are captured through a private pipe,
not printed. URLSession rejects redirects. No browser, remote login or public server
is involved in the panel.

`pair-keychain` uses macOS Security.framework generic-password entries with service
`dev.pair-companion.providers`, a validated account and device-local accessibility.
The stdin/stdout protocol is in [INTERFACES.md](../INTERFACES.md). Only `get` may output the secret;
other operations return boolean state or an OSStatus. There is no shell invocation,
plaintext credential fallback or key in argv. Removing a provider does not silently
delete its Keychain entry: the user can delete that key explicitly before removing
the configuration.

`python3 native/test_keychain.py` checks the helper contract using only disposable,
public fixtures and removes its own records. It never examines real provider keys.
Build success is not full native UX acceptance: a visual walkthrough, input/error
states and an ordinary host-chat round trip are required before release.
