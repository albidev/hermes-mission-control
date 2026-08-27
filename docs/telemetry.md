# Telemetry

Mission Control is a local-first dashboard: all of its data comes from a small local **telemetry sidecar** (Python stdlib + `psutil`) that runs next to your Hermes agent. The frontend never talks to a remote backend — it reads local state through authenticated `/api/local/*` endpoints.

## Components

| Component | Path | Port | Stack |
|-----------|------|------|-------|
| Telemetry server | `server/local_telemetry_server.py` | `8765` | Python stdlib + psutil |
| Frontend | `src/` | `5174` | React + Vite + TypeScript + Tailwind |

All data flows through `/api/local/*`. In development, Vite proxies those requests to the telemetry server. The telemetry server binds to `0.0.0.0` and requires a bearer token on every request (see [`SECURITY.md`](../SECURITY.md)).

The sidecar does **not** hot-reload: restart it after any backend change.

## Bind address and port

The telemetry server reads its bind address and port from environment variables at startup:

| Variable | Default | Purpose |
|----------|---------|---------|
| `MISSION_CONTROL_LOCAL_TELEMETRY_HOST` | `0.0.0.0` | Bind address (canonical name) |
| `MISSION_CONTROL_LOCAL_TELEMETRY_PORT` | `8765` | Bind port (canonical name) |
| `TELEMETRY_BIND_HOST` | — | Legacy alias for the host |
| `TELEMETRY_BIND_PORT` | — | Legacy alias for the port |

The canonical `MISSION_CONTROL_LOCAL_TELEMETRY_*` names take precedence over the legacy `TELEMETRY_BIND_*` aliases when both are set. An invalid port (non-integer or out of `1..65535`) aborts startup with a clear error instead of silently falling back to the default.

On Linux systemd deployments, set the canonical pair in the service `EnvironmentFile` (see `.env.example`).

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
