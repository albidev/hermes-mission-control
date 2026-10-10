# Tool inventory

The Tools route displays the configurable Hermes toolsets as the Hermes dashboard API reports them: `GET /api/tools/toolsets` on `hermes dashboard` (default `127.0.0.1:9119`, proxied by Vite under `/api`).

Mission Control does not compute tool availability itself. Core owns both signals, and only the dashboard process can resolve them correctly:

| Field shown | Core field | Meaning |
|-------------|------------|---------|
| *available* / *disabled* | `enabled` | The toolset's switch on its configuration platform (`hermes tools`) |
| *needs key* | `configured` | Credentials present in the profile's secret scope |
| tool badges | `tools` | The real tools the toolset resolves to, including plugin toolsets |

A toolset card shows its first 8 tools and the number hidden; a search hit beyond the first 8 is shown first.

The inventory is read-only in Mission Control. Enable toolsets and add credentials with `hermes tools` or the Hermes desktop app.

If the dashboard API is down or rejects the token, the route keeps the last loaded inventory and reports the refresh error.
