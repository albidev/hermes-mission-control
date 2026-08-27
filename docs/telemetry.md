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

On Linux systemd deployments, set the canonical pair in the service `EnvironmentFile` (see `.env.example`).

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

## Knowledge vault

The Knowledge page scans a local Markdown vault and exposes it through `/api/local/knowledge` (snapshot) and `/api/local/knowledge/file` (single note content).

### Vault path resolution

The vault root is resolved by one canonical function (`_knowledge_vault_root()` in `server/local_telemetry_server.py`), used for scanning, display, fallback payloads, and file reads:

1. `MISSION_CONTROL_VAULT_PATH` (canonical; supports `~` expansion);
2. `HERMES_OBSIDIAN_VAULT` (legacy alias, kept for compatibility);
3. platform default: `~/Documents/Hermes` on macOS, `~/wiki` on Linux.

On Linux the default is `~/wiki`; set `MISSION_CONTROL_VAULT_PATH` in `.env` to point at an existing vault (e.g. `~/wiki`) when the default does not match your layout.

### Path safety

API responses never include absolute home paths: `sourcePath` and `vaultPath` are rendered home-relative (`~/wiki/notes.md`). When the vault is unavailable the fallback payload still reports a valid display path for the platform — it never fabricates a macOS path on Linux. File reads are restricted to the vault root and `~/.hermes` core files; anything outside returns 403.

## Provider usage (CodexBar)

The **Provider usage** overview card shows live cloud limits and balances for Codex, Ollama Cloud, and OpenRouter. The numbers come from [CodexBar](https://github.com/steipete/codexbar), a macOS menu-bar CLI that reads each provider's usage/credits.

### Data flow

```
CodexBar (codexbar usage --provider <p>)
        │  --json --no-color
        ▼
provider-usage script OR telemetry sidecar
        │  sanitized → providers[]
        ▼
~/.hermes/cache/mission-control-provider-usage.json
        │
        ▼
GET /api/local/provider-usage  (telemetry :8765)
        │
        ▼
src/components/overview/ProviderUsagePanel.tsx  → gauges
```

The frontend polls `loadProviderUsage()` every **60s** (`ProviderUsagePanel` `useEffect` + `setInterval`).

### Two ways the cache is produced

1. **Cache-first (preferred):** the telemetry server reads `~/.hermes/cache/mission-control-provider-usage.json` (`collect_provider_usage`). If the file is present and valid, it returns it directly — no CodexBar call at request time.
2. **Live fallback:** if the cache is missing or corrupt, the sidecar invokes `codexbar usage` for each provider on demand, sanitizes the output, and returns it.

There is also a standalone script to refresh the cache outside the sidecar:

```bash
scripts/update-provider-usage.sh        # writes ~/.hermes/cache/mission-control-provider-usage.json
scripts/local/update-provider-usage.sh  # identical variant
```

These read `MISSION_CONTROL_CACHE_DIR` (default `~/.hermes/cache`). A scheduler (macOS LaunchAgent, Linux systemd timer, or cron) can run it every 60s so the dashboard always has a fresh cache without paying a CodexBar call per request.

### Ollama requires the web source

Ollama's API key path exposes **no usage data**. CodexBar must read the Ollama web dashboard through Chrome cookies, so the Ollama provider is invoked with:

```bash
codexbar usage --provider ollama --source web --json --no-color
```

Both the script and the sidecar force `--source web` for `ollama` (the `local/` script documents this in its header comment). **Do not** drop this flag or Ollama returns no usage.

### Sanitized provider shape

Each provider is reduced to a small, UI-safe object:

```json
{
  "provider": "codex",
  "available": true,
  "source": "cli",
  "updatedAt": null,
  "primary":   { "usedPercent": 12, "resetsAt": "...", "windowMinutes": 240 },
  "secondary": { "usedPercent": 2,  "resetsAt": "...", "windowMinutes": 10080 },
  "tertiary":  null,
  "pace": null,
  "openRouter": null,
  "creditsRemaining": null,
  "resetCreditsAvailable": null
}
```

- `primary` / `secondary` / `tertiary` map to CodexBar's usage windows (session / weekly / …). The UI renders `Session` (primary) and `Weekly` (secondary) gauges.
- `openRouter` is populated only for `openrouter` (balance + usage).
- `creditsRemaining` / `resetCreditsAvailable` only for `codex`.
- On error, `available` is `false` and `error` carries a short (≤240 char) message.

### Gauges

`ProviderUsagePanel.tsx` renders **two horizontal bars** per card (Session + Weekly) — not circular. Color semantics: blue normal (<60%), amber warning (≥60%), red error (≥85%). Missing data renders as `—`, never a fabricated `0%`. OpenRouter shows only its balance, with no artificial gauge.

### Troubleshooting

- **Ollama shows empty / `—`:** likely a Keychain denial. CodexBar's web source uses Chrome cookies; a macOS Keychain prompt denial triggers a **6h cooldown**. Refresh the cookie:
  ```bash
  codexbar cookie refresh --provider ollama --allow-keychain-prompt --json
  ```
  then click **Consenti** on the Keychain prompt.
- **Cache stale after a fix:** if the sidecar keeps serving an old cache, the cache file is read first. Regenerate it with `scripts/update-provider-usage.sh`, or delete `~/.hermes/cache/mission-control-provider-usage.json` to force the live fallback.
- **Zombie telemetry process:** after a restart, confirm the sidecar is running from the repo path, not from a deleted inode. Use `lsof -i :8765` to check the actual process/path.
