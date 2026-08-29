# Telemetry

Mission Control is a local-first dashboard: all of its data comes from a small local **telemetry sidecar** (Python stdlib + `psutil`) that runs next to your Hermes agent. The frontend never talks to a remote backend — it reads local state through authenticated `/api/local/*` endpoints.

## Components

| Component | Path | Port | Stack |
|-----------|------|------|-------|
| Telemetry server | `server/local_telemetry_server.py` | `8765` | Python stdlib + psutil |
| Frontend | `src/` | `5174` | React + Vite + TypeScript + Tailwind |

All data flows through `/api/local/*`. In development, Vite proxies those requests to the telemetry server. The telemetry server binds to `127.0.0.1` (loopback) by default and requires a bearer token on every request (see [`SECURITY.md`](../SECURITY.md)).

The sidecar does **not** hot-reload: restart it after any backend change.

## Bind address and port

The telemetry server reads its bind address and port from environment variables at startup:

| Variable | Default | Purpose |
|----------|---------|---------|
| `MISSION_CONTROL_LOCAL_TELEMETRY_HOST` | `127.0.0.1` | Bind address (canonical name) |
| `MISSION_CONTROL_LOCAL_TELEMETRY_PORT` | `8765` | Bind port (canonical name) |
| `TELEMETRY_BIND_HOST` | — | Legacy alias for the host |
| `TELEMETRY_BIND_PORT` | — | Legacy alias for the port |

The canonical `MISSION_CONTROL_LOCAL_TELEMETRY_*` names take precedence over the legacy `TELEMETRY_BIND_*` aliases when both are set. An invalid port (non-integer or out of `1..65535`) aborts startup with a clear error instead of silently falling back to the default.

### Security implications of the bind address

Mission Control is a **local operator dashboard**: it defaults to loopback so it is unreachable from the network unless you explicitly opt in. This matters on Linux hosts with LAN interfaces, containers, VPNs, or Tailscale peers — `0.0.0.0` would expose the dashboard (and its data endpoints) to every interface the machine has.

- Local-only operation: leave `MISSION_CONTROL_LOCAL_TELEMETRY_HOST` unset (or set it to `127.0.0.1`). The server listens on loopback only.
- Tailscale/LAN exposure: set `MISSION_CONTROL_LOCAL_TELEMETRY_HOST=0.0.0.0` (or the legacy `TELEMETRY_BIND_HOST`) explicitly. This is the opt-in signal. The dashboard is still protected by the bearer token, but the token is the only boundary between your network peers and the data.
- A reverse proxy (Caddy, nginx, `tailscale serve`) can expose the dashboard without changing the bind: keep the telemetry server on loopback and proxy to it.

On Linux systemd deployments, set the canonical pair in the external
`~/.hermes/mission-control.env` file (see `.env.example` for the variable list).

## CORS origin enforcement

Browsers enforce the same-origin policy: a page served from one origin cannot read responses from another unless the server grants it via CORS headers. The telemetry server's behavior depends on `MISSION_CONTROL_ALLOWED_ORIGIN`:

| Setting | Behavior |
|---------|----------|
| Unset (dev default) | Incoming `Origin` is mirrored back (`Access-Control-Allow-Origin: <origin>`), so browser sidecars work across Tailscale/LAN without extra configuration. |
| Set to `http://host:port` | **Only that exact origin** receives CORS headers. Any other `Origin` gets none, so browsers block the cross-origin response. |

Examples:

```bash
# Dev / Tailscale: mirror whatever origin the browser sends
MISSION_CONTROL_ALLOWED_ORIGIN=

# Hardened: only this exact frontend origin may read responses
MISSION_CONTROL_ALLOWED_ORIGIN=http://100.84.148.17:5174
```

The comparison is exact — scheme, host, and port must all match. Requests without an `Origin` header (curl, same-origin fetches, non-browser clients) are not subject to CORS and pass through as usual; CORS never protects data without authentication, it only tells the browser which origins may read the response.

### Vite dev server: `host: true`

The Vite dev server (`vite.config.ts`) uses `host: true`, which makes it listen on **all interfaces** — the same exposure surface as the telemetry server's old `0.0.0.0` default. It is a dev tool, so it does not have a loopback default, but on a shared/LAN host you should treat `5174` as network-visible:

- Vite filters incoming requests by `Host` header via `allowedHosts` (default `localhost,127.0.0.1`, extended with `MISSION_CONTROL_DEV_HOSTS`). A peer whose address is not listed gets rejected at the Vite layer ("due to access control checks") before any request reaches the backend.
- `host: true` is required for Tailscale/LAN access and for the bundled systemd unit (`vite --host 0.0.0.0`). For local-only work, run plain `pnpm dev` (loopback) or remove `--host` from the unit.
- The Vite dev server is a development surface: for production, serve the built `dist/` behind a static host or reverse proxy instead of exposing the dev server.

## Hermes home and profile resolution

The sidecar resolves every Hermes state path (state DB, sessions, logs,
skills, config, cache, vault-brain candidates) through
`server/hermes_paths.py`, which mirrors the Hermes core launcher:

1. `HERMES_HOME` set and already profile-shaped (`<root>/profiles/<name>`) → used verbatim.
2. Sticky active profile (`<root>/active_profile` contains a name other than `default`) → `<root>/profiles/<name>`.
3. `HERMES_HOME` set (non profile-shaped) → used verbatim.
4. Platform default → `~/.hermes`.

The bash launchers (`scripts/run-local-telemetry.sh`,
`scripts/run-dashboard-api.sh`) use the twin `resolve_hermes_home` from
`scripts/lib/env.sh` with the same precedence, so the server process is
launched with the same home the server itself resolves. This keeps Mission
Control reading the correct Hermes state when Hermes runs from a non-default
home or a named profile.

## Endpoints

The telemetry server exposes a set of read-only `/api/local/*` endpoints. Representative routes:

- `/api/local/health` — liveness
- `/api/local/system` — system metrics (CPU, disk, thermal)
- `/api/local/sessions` / `/api/local/sessions/usage` — session & usage
- `/api/local/agents` and `/api/local/agents/trace` — agent traces
- `/api/local/provider-usage` — **provider usage via CodexBar** (see below)
- `/api/local/status`, `/api/local/model/info` — agent runtime state
- `/api/local/cron/jobs`, `/api/local/config`, `/api/local/tools`, `/api/local/skills`
- `/api/local/logs`
- `/api/local/chat/last`, `/api/local/chat/whiteboard` — chat + tldraw bridge
- `/api/local/knowledge`, `/api/local/knowledge/file` — vault knowledge (see below)

The frontend consumes these through `src/lib/hermes-api.ts` (`loadProviderUsage`, etc.) and `src/lib/mission-control-store.tsx`.

## Thermal telemetry

`/api/local/system` includes a `thermal` object produced by `collect_thermal_snapshot()`. The backend is platform-specific and never requires interactive or passwordless sudo for normal operation.

### Linux

Fallback order in `_collect_linux_thermal_snapshot()`:

1. **`/sys/class/thermal`** — kernel thermal zones (`thermal_zone*`). Purely passive sysfs reads, no privileges. The lowest zone temperature (in °C) is reported as `thermalPressure`.
2. **`lm-sensors`** — when sysfs has no usable zone, the `sensors -u` binary is invoked without sudo. The lowest `temp*_input` value is reported.
3. **Unavailable** — no supported sensor at all → a structured `unavailable` state (`source: "unavailable"`), never a backend failure.

| Field | Meaning |
|-------|---------|
| `thermalPressure` | Lowest temperature in °C (Linux), or 0–100 pressure index (macOS). `null` when unavailable. |
| `thermalLevel` | Text level (macOS only: `nominal`…`extreme`). `null` on Linux. |
| `levelSource` | Backend that produced `thermalLevel` (`powermetrics`), `null` otherwise. |
| `source` | `sysfs-thermal`, `lm-sensors`, `powermetrics`, `unavailable`, or `null` when no data was read. |
| `error` | Diagnostic message; `null` on success. A missing sensor sets `source: "unavailable"` with an explanatory `error` — this is a normal state on hosts without thermal hardware, not a failure. |

The Overview UI distinguishes the states: `source === 'unavailable'` renders "Unavailable" (with the diagnostic as a caption), a numeric `thermalPressure` renders °C with a threshold bar, and macOS falls back to the discrete level meter.

### macOS (unchanged)

`powermetrics` needs passwordless sudo and on macOS 26 / Apple Silicon only exposes thermal pressure as a text level (`Nominal`/`Low`/`Moderate`/`Heavy`/`Extreme`), mapped to a normalised 0–100 index. Returns nulls when powermetrics/sudo is unavailable.

## Knowledge vault

The Knowledge page scans a local Markdown vault and exposes it through `/api/local/knowledge` (snapshot) and `/api/local/knowledge/file` (single note content).

### Vault path resolution

The vault root is resolved by one canonical function (`_knowledge_vault_root()` in `server/local_telemetry_server.py`), used for scanning, display, fallback payloads, and file reads:

1. `MISSION_CONTROL_VAULT_PATH` (canonical; supports `~` expansion);
2. `HERMES_OBSIDIAN_VAULT` (legacy alias, kept for compatibility);
3. platform default: `~/Documents/Hermes` on macOS, `~/wiki` on Linux.

On Linux the default is `~/wiki`; set `MISSION_CONTROL_VAULT_PATH` in the
external `~/.hermes/mission-control.env` file (or export it) to point at an
existing vault when the default does not match your layout.

### Path safety

API responses never include absolute home paths: `sourcePath` and `vaultPath` are rendered home-relative (`~/wiki/notes.md`). When the vault is unavailable the fallback payload still reports a valid display path for the platform — it never fabricates a macOS path on Linux. File reads are restricted to the vault root and `~/.hermes` core files; anything outside returns 403.

## Web Push (optional)

Web Push lets the browser deliver notifications while the app is in the
background or closed. It is **opt-in**: the base install (`server/requirements.txt`)
does not include the required packages, and the sidecar runs fine without them.

Enable it with:

```bash
python3 -m pip install -r server/requirements-push.txt   # pywebpush + websockets
```

then set `MISSION_CONTROL_VAPID_PUBLIC_KEY`, `MISSION_CONTROL_VAPID_PRIVATE_KEY`
and `MISSION_CONTROL_VAPID_CONTACT` (a `mailto:` or `https:` sender identity —
**required**; push stays disabled without it) in the environment file. See
`.env.example` for the exact names.

The sidecar reports the push state on every health probe:

```json
{"ok": true, "service": "mission-control-local-telemetry", "source": "local-psutil",
 "push": {"enabled": false, "reason": "vapid_not_configured",
          "missingConfig": ["MISSION_CONTROL_VAPID_CONTACT"]}}
```

- `reason: "vapid_not_configured"` — keys or contact missing (`missingConfig` lists them): intentional disablement.
- `reason: "missing_dependency"` — the optional packages are not installed in the interpreter running the sidecar (`missingDependencies` lists them).
- `reason: "ok"` — configured and importable; per-subscription delivery failures are still possible and are counted in `send_push`'s `failed` field.

Related endpoints: `/api/local/push/vapid-public-key` (serves the public key to
the client), `/api/local/push/subscriptions` (store/list/delete), and
`/api/local/push/send` (test delivery — returns the same `reason` when disabled).

## Provider usage (CodexBar + Nous Portal)

The **Provider usage** overview card shows live cloud limits and balances for Codex, Ollama Cloud, OpenRouter, and Nous Portal. CodexBar supplies the first three providers; the telemetry sidecar reads Nous Portal account data through the already-authenticated Hermes access token.

### Data flow

```
CodexBar (codexbar usage --provider <p>)
        │  --json --no-color
        ▼
provider-usage writer OR telemetry fallback
        │  normalized → providers[]
        ├──────────────────────────────┐
        │                              │
        ▼                              ▼
CodexBar cache                 Nous Portal adapter
~/.hermes/cache/               auth.json access_token
mission-control-               GET /api/oauth/account
provider-usage.json                   │
        └──────────────┬───────────────┘
                       ▼
GET /api/local/provider-usage  (telemetry :8765)
                       │
                       ▼
src/components/overview/ProviderUsagePanel.tsx
```

The frontend polls `loadProviderUsage()` every **60s** (`ProviderUsagePanel` `useEffect` + `setInterval`).

### Data sources and cache behavior

1. **CodexBar providers:** the telemetry server reads `~/.hermes/cache/mission-control-provider-usage.json` (`collect_provider_usage`). If the file is present and valid, it uses the cached CodexBar entries; otherwise it invokes CodexBar for `codex`, `ollama`, and `openrouter`.
2. **Nous Portal:** the sidecar reads the current `providers.nous.access_token` from the active/profile-aware `auth.json` and performs a read-only `GET /api/oauth/account`. If the access token is expired, it delegates refresh to the existing `hermes portal info` command and then re-reads `auth.json`; the sidecar never implements the OAuth refresh exchange or rotates refresh tokens itself.
3. **Provider-agnostic boundary:** every entry returned by `/api/local/provider-usage` exposes `windows`, `balances`, and `metrics`. The frontend does not depend on CodexBar's raw provider-specific fields.

The standalone cache writer is still useful for refreshing the CodexBar entries outside request time:

```bash
scripts/update-provider-usage.sh        # profile-aware writer
scripts/local/update-provider-usage.sh  # compatibility wrapper
```

These read `MISSION_CONTROL_CACHE_DIR` (defaulting to the resolved Hermes cache directory). A scheduler can run the writer every 60s so CodexBar data stays fresh without paying a CodexBar call per request. Nous data is fetched by the telemetry sidecar from the already-authenticated Portal session.

### Ollama requires the web source

Ollama's API key path exposes **no usage data**. CodexBar must read the Ollama web dashboard through Chrome cookies, so the Ollama provider is invoked with:

```bash
codexbar usage --provider ollama --source web --json --no-color
```

Both the script and the sidecar force `--source web` for `ollama` (the `local/` script documents this in its header comment). **Do not** drop this flag or Ollama returns no usage.

### Sanitized provider shape

Each provider is reduced to the same small, UI-safe contract:

```json
{
  "provider": "nous",
  "available": true,
  "source": "portal-account",
  "updatedAt": "...",
  "stale": false,
  "plan": "Free",
  "renewsAt": "...",
  "windows": [
    { "id": "subscription", "label": "Subscription", "usedPercent": 42, "remaining": 12, "total": 20, "unit": "USD" }
  ],
  "balances": [
    { "id": "topup_remaining", "label": "Top-up remaining", "value": 9.87, "currency": "USD" }
  ],
  "metrics": []
}
```

- `windows` contains quota/period usage such as CodexBar's session/weekly windows or Nous's monthly subscription allowance.
- `balances` contains monetary or credit balances.
- `metrics` contains provider counters such as Codex reset credits.
- On error, `available` is `false`, the arrays remain present, and `error` carries a short (≤240 character) message.
- Nous's `stale` flag is `true` only when the Portal request failed but a previous valid in-process snapshot is being served.

### Gauges

`ProviderUsagePanel.tsx` renders horizontal usage bars for entries in `windows` and numeric balances for entries in `balances`. Missing data renders as `—`, never a fabricated `0%`. Nous uses a billing-oriented card: subscription usage is shown as a window when the Portal supplies a monthly denominator, while subscription/top-up/total values remain explicit balances.

### Troubleshooting

- **Nous Portal unavailable:** telemetry reads only the current `providers.nous.access_token` from the active Hermes `auth.json`. If it is expired, telemetry delegates the refresh to `hermes portal info`; it never implements or rotates the refresh token itself. Re-authenticate through Hermes (`hermes portal` / `hermes auth add nous`) if that delegated refresh fails.
- **Nous endpoint changes:** the Portal account endpoint is currently used by Hermes but is not a public CodexBar usage contract. The adapter is isolated in `server/nous_portal_usage.py`; update that adapter and its fixture if Nous changes the response shape.
- **Cache stale after a fix:** CodexBar entries are cache-first. Regenerate them with `scripts/update-provider-usage.sh`; Nous is fetched by the sidecar with its own 60-second in-process snapshot.
- **Ollama shows empty / `—`:** likely a Keychain denial. CodexBar's web source uses Chrome cookies; a macOS Keychain prompt denial triggers a **6h cooldown**. Refresh the cookie:
  ```bash
  codexbar cookie refresh --provider ollama --allow-keychain-prompt --json
  ```
  then click **Consenti** on the Keychain prompt.
- **Zombie telemetry process:** after a restart, confirm the sidecar is running from the repo path, not from a deleted inode. Use `lsof -i :8765` to check the actual process/path.
