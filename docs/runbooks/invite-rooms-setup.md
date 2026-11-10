# Invite Rooms: operator setup and troubleshooting

This runbook prepares Mission Control's invite-room sidecar. It is **not** a gateway installer, a public reverse-proxy recipe or permission to restart a shared gateway. Start with the [user journey](../invite-rooms-user-guide.md); headless callers also need the [agent runbook](invite-rooms-agent.md).

## Readiness and safety gates

The branch implements the owner/guest flow and gated history, but a complete two-installation native-process journey and cross-network deployment remain separate acceptance gates. The config template below can be validated without making it an accepted deployment.

Before any write, the operator must identify and authorize:

- The MC checkout/revision, sidecar service, native installation/home and owner profile on **each** side. The currently advertised native RoomLink contract requires the default-profile owner; canonical attachment to a named profile alone does not establish RoomLink support.
- The actual existing native installation ID, not a generated example or one copied from the other machine.
- Each loopback listener/destination and the real native HTTP API authority. Do not derive an HTTP destination from a WebSocket URL or copy another operator's ports.
- The invitation origin each owner will pin and the independent channel for comparing the SAS.
- Separate approval for changes to a shared gateway config, service restarts and network exposure. Documentation is not that approval.

Never modify native room/session databases, synthesize catalogs, copy consent between installations, repair identity files, or substitute guessed IDs/URLs. Stop on conflicting ownership, missing authority or unexpected state. Do not enumerate or modify unrelated profiles.

## 1. Identify the two services

| Surface | Owner | Role |
|---|---|---|
| Native gateway | Hermes | Canonical Room, capabilities, log and scoped execution/grants |
| MC telemetry sidecar | Mission Control | Owner HTTP routes, invitation/consent stores, pinned control and data-plane lifecycle |
| MC frontend/proxy | Mission Control | Browser UI and authenticated forwarding to the correct backend |

A healthy frontend or `GET /paired-peers` does not prove the data plane started. Native `room_link.enabled: true` does not configure MC. Keep these readiness checks separate.

Use the repository [quick start](../../README.md#quick-start) and your [deployment runbook](linux-deployment.md) for the installation you actually manage. Make `gateway.hosted_room_peer` and the canonical `hermes_cli.gateway_runtime` / `gateway_client` helpers importable by the sidecar's interpreter; the source checkout alone is not an installed Python dependency.

## 2. Verify native readiness without creating a Room

Through the existing owner-authenticated native socket, call:

```json
{"jsonrpc":"2.0","id":"rooms-preflight","method":"groups.capabilities","params":{"profile":"default"}}
```

Require a real `room_link` catalog with `enabled: true` and the required native Room/peer methods. The raw JSON is an RPC envelope, not an unauthenticated HTTP request; use the canonical client/session-ticket handshake. Do not paste a long-lived token into a WebSocket URL.

For a source-checkout diagnostic, the following Python probe uses the same attach-only adapter as MC. Set the three variables to your **explicitly selected** checkout, installed interpreter and native default home. It requests a transient authenticated socket and reads capabilities; it does not start/restart the gateway or create a Room:

```bash
export MC_REPO=/absolute/path/to/hermes-mission-control
export HERMES_CORE_DIR=/absolute/path/to/hermes-agent
export MC_PYTHON=/absolute/path/to/installed/python
export HERMES_NATIVE_HOME=/absolute/path/to/native-default-home
PYTHONPATH="$MC_REPO/server:$HERMES_CORE_DIR" "$MC_PYTHON" - <<'PY'
import asyncio, json, os, re
from pathlib import Path
# Scope imports to the explicitly selected default home, never an ambient profile.
home = Path(os.environ['HERMES_NATIVE_HOME'])
os.environ['HERMES_HOME'] = str(home)
os.environ['HERMES_PROFILE'] = 'default'
os.environ['HERMES_DISABLE_LAZY_INSTALLS'] = '1'
from invite_rooms.routes import _connect_native_rpc
identity = (home / 'install_id').read_text().strip()
assert re.fullmatch(r'[0-9a-f]{32}', identity), 'STOP: invalid native install_id'
config = {'profile_home': str(home), 'profile': 'default',
          'installation_id': 'install:' + identity}
async def main():
    async with _connect_native_rpc(config) as rpc:
        caps = await rpc('groups.capabilities', {'profile': 'default'})
        link = caps.get('room_link', {})
        required = {'groups.peer.invite', 'groups.peer.register', 'groups.log'}
        result = {'room_link_enabled': link.get('enabled') is True,
                  'reason': link.get('reason'),
                  'required_methods': required <= set(caps.get('methods', []))}
        print(json.dumps(result))
        assert result['room_link_enabled'] and result['required_methods'], 'STOP: native not ready'
asyncio.run(main())
PY
```

**Stop on failure.** This is a repository-version-bound diagnostic, not a general-purpose remote RPC SDK. If imports or the canonical handshake fail, repair the installed dependency/owner selection; never fall back to another dashboard or Bearer endpoint.

### Native endpoint configuration

`endpoint_required` means the native gateway advertises no usable endpoint. Read its actual API Server configuration and verify that listener first. If the operator authorizes a native config change, back up that installation's config and set its explicit RoomLink endpoint through the supported CLI, for example:

```bash
# EXAMPLE ONLY: 8642 must already be this installation's verified native API port.
hermes --profile default config set gateway.room_link_url http://127.0.0.1:8642
hermes --profile default config get gateway.room_link_url
```

The CLI can warn that this key is unrecognized even when the gateway loader supports it. Verify the selected installation's effective `load_gateway_config().room_link_url`, not just a successful YAML write. This setting is restart-scoped: only an explicitly authorized operator may restart that shared gateway, after accounting for in-flight turns and affected adapters. Then repeat the capability probe in the **running** process.

Loopback advertisement is local-only. It neither creates an Internet endpoint nor proves reachability between two machines. Do not change the bind to a wildcard or publish the native API as a shortcut.

## 3. Back up before provisioning MC

Keep the owner-private state outside the checkout. Stop only the affected sidecar, with authorization, before taking a consistent filesystem snapshot of SQLite files, journals and key material. For an online SQLite copy, use SQLite's backup mechanism rather than copying the main file without its journal.

Example for an already stopped sidecar (select real paths first; `BACKUP` must be outside `STATE`):

```bash
umask 077
STATE=/absolute/private/invite-rooms
BACKUP=/absolute/private/backups/invite-rooms-before-change
# BACKUP's private parent must already exist; refuse to overwrite a backup.
mkdir -m 700 "$BACKUP"
if [ -e "$STATE" ]; then
  cp -Rp "$STATE" "$BACKUP/state"
else
  printf 'Fresh install: no existing MC state to copy.\n'
fi
```

Verify the backup opens/parses before changing the source. Preserve the pairing journal, identity bundle, encrypted-store keys and consent ledger along with the databases; restoring ciphertext alone is not a recovery plan. Do not overwrite an existing backup or print its contents.

## 4. Provision the private configuration in stages

`MISSION_CONTROL_ROOM_INVITES_CONFIG` points to one JSON file, mode `0600`, under an owner-controlled `0700` directory. `peer_registry_path` initially contains `[]` in a private `0600` file; it is a projection, not permission to preseed trusted peers.

The base owner fields are `store_path`, `installation_id`, `profile` and `peer_registry_path`. Pairing actions additionally need the `pairing` identity/journal/projection paths and a compatible native catalog. Actual pairing operations initialize their owned identity; canonical native attachment never creates the native installation identity.

The following is a **renderable full-config template**, not a ready-to-run file. Replace every `__...__` marker, review the example ports against your real topology and select distinct paths/ports for the other installation. Its remote peer key must be the actual product identity obtained during pairing, not an invented trusted peer. While bootstrapping, pairing may be locally confirmed but its `pair_ack` can remain pending until both complete control listeners and destinations are configured.

```json
{
  "store_path": "__STATE__/invites.sqlite",
  "installation_id": "install:__INSTALL_ID__",
  "profile": "default",
  "peer_registry_path": "__STATE__/peers.json",
  "native_rpc": {"profile_home": "__NATIVE_HOME__"},
  "target_http": {"base_url": "http://127.0.0.1:__API_PORT__", "profile": "default"},
  "data_listen": {"host": "127.0.0.1", "port": 18741},
  "invitation_origin": "https://owner-a.example",
  "history_enabled": false,
  "pairing": {
    "identity_dir": "__STATE__/identity",
    "journal_dir": "__STATE__/pairing-journal",
    "peers_path": "__STATE__/peers.json",
    "control_listen": {"host": "127.0.0.1", "port": 18740},
    "control_endpoints": {"__REMOTE_PEER_ID__": {"host": "127.0.0.1", "port": 18750}},
    "target_listeners": {},
    "data_endpoints": {}
  },
  "joining": {
    "store_dir": "__STATE__/joining",
    "identity_dir": "__STATE__/identity",
    "hosts_path": "__STATE__/hosts.json",
    "origins": {"__REMOTE_PEER_ID__": "https://owner-b.example"},
    "control_endpoints": {"__REMOTE_PEER_ID__": {"host": "127.0.0.1", "port": 18750}}
  }
}
```

Template contract:

- `__STATE__`: an absolute private MC state directory, distinct on each installation.
- `__NATIVE_HOME__`: that installation's existing default native home. Its `install_id` must be a regular, single-link `0600` file under a `0700` home; do not re-key it.
- `__INSTALL_ID__`: exactly the existing 32-lowercase-hex native installation ID, without its `install:` prefix.
- `__API_PORT__`: the separately verified local native HTTP API port, not a transformed WebSocket destination.
- `__REMOTE_PEER_ID__`: the peer ID the product returns; an address-map entry does not create trust.
- `18740` / `18741` / `18750`: illustrative local control/data/other-owner control ports, not deployment defaults. Check ownership and collisions before binding them. Separate-host loopback addresses do not magically connect to each other.
- `https://owner-a.example` / `https://owner-b.example`: **placeholder identifiers**, not real URLs to share. The issuer's `invitation_origin` must exactly match the receiver's `joining.origins[issuer_peer_id]`. No path/query/fragment/userinfo is permitted. These pins are separate from networking, CORS and `gateway.room_link_url`.

Omitted topology maps are not equivalent to `{}`. Empty `target_listeners` and `data_endpoints` support a deny-all cold start, but authorize no data route. Populate only actual product-derived adapter/scope mappings when the approved flow provides them, at an explicit sidecar restart boundary; do not guess a `link-...` ID to complete the template. This manual topology step is an operator integration boundary, not automatic discovery.

Keep `history_enabled: false` until the paired identity bundle exists on each side. Once initialized, enabling it requires operator approval, a complete topology and a sidecar restart to install the host history handlers. It does **not** change a Room's immutable `history_policy` or sign owner/guest consent. Set it on each participating side that needs the history flow.

Use one `native_rpc` form only. `profile_home` is the canonical attach-only form; the legacy `{endpoint, token_path}` form is for an explicitly compatible old deployment, never an automatic fallback.

### Optional controls are not the same policy

| Setting | Scope and behavior |
|---|---|
| `VITE_ROOMS_EXTENDED_TTL=1` | Frontend env flag showing extended grant-duration presets; it does not change target enforcement. |
| `lobby_grant_ttl_extended_presets` | Private host JSON boolean, default false, controlling acceptance of extended lobby grant-duration hints. |
| `joining.grant_ttl_seconds` | Optional target-local integer grant lifetime, 60..86400 seconds. |
| `joining.grant_ttl_max_seconds` | Optional target-local integer maximum, 60..86400; configured lifetime must not exceed it. |
| `joining.grant_status_ttl_seconds` | Optional status horizon; requires an explicit grant lifetime, is at least that lifetime and at most 2592000 seconds. |
| `joining.host_label`, `joining.own_bot_id` | Optional local guest-display/context fields, not authority to select another peer or native profile. |
| `original_frontdoor_url` | Optional already-approved original frontdoor whose validated loopback bind must exactly equal `data_listen`; not a way to bypass scope routing or provision a public listener. |

Unset target lifetime fields retain the existing target policy; do not infer an
applied duration from a host's hint. A requested duration, invitation expiry,
execution-grant expiry and status-read horizon are distinct. Longer choices do
not establish renewal, surviving-restart membership or cross-owner TTL delivery
without separate behavioral verification.

## 5. Load the intended config and restart narrowly

Set the sidecar's `MISSION_CONTROL_ROOM_INVITES_CONFIG` through your existing service environment. Inspect its actual env file/launch command: the service's env-file loader may override plist values. Do not commit private JSON, tokens, keys or runtime data.

After authorization, restart **only the affected MC sidecar** using your deployment's actual service:

```bash
# macOS, only if this is the LaunchAgent your installation owns:
launchctl kickstart -k "gui/$(id -u)/ai.hermes.mission-control-telemetry"
# Linux, only if installed with the repository's user service:
systemctl --user restart hermes-mission-control-telemetry.service
```

Do not run both platform commands. A changed plist/unit definition needs its deployment-specific reload, not just a process restart. Do not restart the dashboard/session host or a shared native gateway to apply MC JSON. Plain frontend source changes use Vite HMR and need no restart.

Record service PID and log byte offsets **before** restart. Afterwards, verify a changed sidecar PID, an unchanged session-host PID, a fresh startup banner and only the newly appended log bytes. A warning such as `data plane configuration is incomplete` means it did not activate, even if an owner HTTP route answers 200.

## 6. Verify in layers, then perform the owner journey

Use the existing local MC credential without displaying it or moving it into a URL:

1. Without authentication, `/api/local/room-invites/paired-peers` must refuse access. With the correct owner credential it returns JSON containing `peers`; an empty list is a legitimate fresh state.
2. Check both the direct sidecar and frontend proxy. A 200 HTML fallback is not an API success.
3. Confirm listener ownership and the absence of a data-plane-disabled startup warning.
4. Complete the [pairing and Room journey](../invite-rooms-user-guide.md), including actual SAS comparison, both participation decisions, native activation and both history decisions.
5. Record the exact native Room ID, original event IDs/order, history policy and admission boundary. Verify the `postjoin` restriction and revocation with owned test data, not someone else's Room.
6. Do not label a local loopback check cross-network or a copied-Core-method test an independent gateway-process E2E.

## 7. Troubleshooting

| Symptom/code | Check / stop condition |
|---|---|
| 401 | The calling owner credential and service env source. Do not disable auth. |
| `forbidden_origin` | Exact `MISSION_CONTROL_ALLOWED_ORIGIN` match for browser requests; invitation pins are a different setting. |
| 404 on feature route | Selected private config, permissions and the route's additional joining/history gate. Not proof of no invitations. |
| `native_unavailable` | Exact home/install binding, compatible helper imports, ready owner socket, fresh ticket/subprotocol and service availability. |
| Native `endpoint_required` | Native advertised endpoint absent/invalid. Provision only an explicitly authorized real endpoint, then operator-controlled restart. |
| `pairing_native_unavailable` | Native catalog/peer contract unavailable. Do not fabricate `enabled` or choose legacy fallback. |
| `invite_origin_unavailable` | Issuer's explicit `invitation_origin`; issuance must fail before minting a secret. |
| `unknown_host` / joining surface absent | One usable joining-role journal link, pinned origin, identity and current projection. Do not rewrite the projection. |
| Pair acknowledgement pending | Both owners' control listeners and the explicit peer-keyed loopback destinations. Confirmation is not transport readiness. |
| `material_channel_unavailable` / activation not ready | Complete running coordinator/registry, actual scoped material and current decisions. No static grant in `peers.json`. |
| History approval/consent 404 | Runtime `history_enabled` and the complete pairing/native/joining config; enabling the gate does not create consent. |
| History forbidden/unavailable | Current admission, canonical membership, authority/epoch, owner approval, guest acceptance and the same pinned relation. No local transcript fallback. |
| `creation_ambiguous` / `registration_ambiguous` | Preserve the existing lobby/native IDs. Inspect and use explicit owner reconciliation; no blind mutating retry. |
| `legacy_schema_recovery_required` | Version 1 has no safe automatic consent/ownership migration. Archive it and choose an owner-approved new store. |
| `schema_too_new` | Do not downgrade or relabel the DB; use compatible code or restore an approved full backup. |

Invite stores now use schema **v3**. A v2 store adds empty history-policy/admission tables; existing Rooms do not gain a policy or consent by inference. Reopening pre-generation v3 admissions also upgrades their durable generation metadata. Back up first; neither migration authorizes access. Version 1 is refused; higher-than-supported schemas remain write-refused.

## Rollback and acceptance record

On rollback, stop the affected sidecar with permission, restore the matching private config **and** coherent state/key/journal backup, then restart that sidecar and verify. Do not restore only the old JSON over migrated state and assume old code can write it. Roll back native gateway config only if you changed it and have separate restart approval. Never delete another owner's peer or consent to tidy up a failed test.

Record revision, platform, real services and endpoints (private record), exact probes, owner decisions, tests actually executed, preserved IDs, cleanup and remaining limitations. Until demonstrated separately, leave full two-native-process activation/recovery, cross-network/VPS topology, physical-phone and Safari acceptance **open**. Parsing this template or passing CI does not close those gates.
