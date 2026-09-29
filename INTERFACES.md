# Internal seams (working contract)

Python package `pair_core`; native shell in `native/`. Root owns config/storage/vault, admin HTTP, MCP and install/build packaging. Separate workers own routing and official CLI adapters. Do not edit another worker's files.

## Configuration
JSON fields use camelCase. Config: `version:1`, `revision:int`, `providers:[]`, `members:[]`, `pair:{codex:{model,effort},claude:{model,effort},projectRoots:[]}`, `limits:{maxTokens:null,timeSeconds:null,budgetUsd:null}`, `jev:{enabled:false,providerId:null,model:"jev-latest"}`. No keys in JSON.

Provider: `{id,name,protocol:"openai"|"anthropic"|"jev",baseUrl,enabled:true,manualModels:[]}`. Route: `{providerId,model,effort:null}`. Member: `{id,label,routes:[Route]}`. IDs are UUID or simple `[A-Za-z0-9_-]` identifiers, model exact strings. Changes preserve host provider and existing jobs' snapshots. `providers` can contain any number of configured supported endpoints, not a singleton.

## Root services
`Store(state_dir: Path)` uses sqlite3. Methods: `config() -> dict`, `save_config(dict) -> dict` (revision increments), `create_job(kind:str,request:dict)->dict`, `update_job(id:str, **fields)->dict`, `job(id:str)->dict|None`, `history()->list`, `event(id:str,kind:str,data:dict)`, `events(id:str)->list`. Job fields: id, kind, status, request, createdAt, updatedAt, result, error, pid, costUsd, reservedUsd; updates may add metadata. Native and MCP share state.

`Vault(state_dir:Path)` methods `get(provider_id)->str|None`, `set(provider_id,key:str)`, `delete(provider_id)`, `has(provider_id)->bool`. Backed by native Keychain helper; fixture test vault separate injectable, never production plaintext fallback. Keychain helper protocol: stdin JSON `{operation:"get"|"set"|"delete"|"has",account,secret?}`; successful stdout JSON `{ok:true,secret?}` only on get; error numeric OSStatus, no secret echo. Service `dev.pair-companion.providers`. Helper path env `PAIR_KEYCHAIN_HELPER` or packaged helper. Native UI never gets saved key value back.

## Routing worker
Own `pair_core/providers.py`, `pair_core/council.py`, `pair_core/jev.py` and relevant tests. `Catalog(vault).models(provider)->list[dict]`: normalized id,name,reasoning?,contextLength?,pricing? and capabilities; no generation invoked. `ProviderRouter(vault).complete(routes,messages,limits,job_id=None,on_event=None)->dict` async: text, providerId, model, usage, costUsd (None when unknown), reservedUsd, attempts, warnings. SDKs provide HTTP/stream parsers; own code is thin routing policy. TTFT 15 seconds for configured Clodex-like endpoint/name; no retry on same route, fallback only before useful output. Honor user parameters where supported, disclose unsupported ones. Finite budget must not treat unknown cost as zero; no hidden SDK retries.

`CouncilRunner(store,vault).run(question,context="",members=None,limits=None)->dict` async: opinions, synthesis, disagreements?, costUsd, reservedUsd, errors and actual trace. Load saved roster snapshot; chosen members may override explicitly; independent opinions, errors represented separately; host synthesis permitted ONLY if labelled pending, do not call it completed. No hardcoded confidence. API route model selection belongs to each member. Subscription members may use explicit `providerId:"codex"|"claude"` and delegate via CLI adapter when enabled.

`JevTools(vault).check(...) / rank(...)` async: typed outputs, uncertainty and usage; configured providerId must point to `protocol:jev`. Missing key is actionable error; no fabricated classifications. Preserve raw input reference and don't delete evidence.

## CLI worker
Own `pair_core/cli.py`, `pair_core/cli_worker.py`, `pair_core/pal_bridge.py`, vendored pinned PAL CLI-only closure and tests. `CliManager(store,vault)` methods async `status()->dict`, `start(agent,prompt,project,model=None,effort=None,mode="subscription",providerId=None,limits=None)->dict` (persisted job, worker executes), `cancel(job_id)->dict`; full result stored, native CLI permission settings not bypassed. Validate project against explicit saved projectRoots. Root will expose job/result polling via Store. No ambient provider keys handed to unrelated agents; API-mode trusted official CLI may receive only its explicitly configured key. Official auth/credentials never read into tools. CLI quota and skill metadata via documented interfaces only.

## Native/admin API (to implement at root)
App starts `python -m pair_core app-server --state <dir> --port 0`, reads one stdout handshake JSON `{port,token}` (token ephemeral, never logs), then sends Authorization Bearer via URLSession only to loopback. Routes: GET `/state` sanitized config + keyPresent + jobs/status; PUT `/config` validated config; POST `/providers/{id}/key` `{key}`; DELETE key; POST `/providers/{id}/models`; GET `/agents`; POST `/jobs` CLI args or council; GET `/jobs/{id}`; POST `/jobs/{id}/cancel`. No CORS/public listener; reject browser Origin and non-loopback Host; failures sanitized, keys not returned.
