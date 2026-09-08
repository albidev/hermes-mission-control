# Mission Control Plugin System

Mission Control (MC) uses a **self-contained plugin architecture**: plugins are discovered at runtime, registered via a manifest, and dispatched without MC knowing their internals.

**Key principle:** MC knows *that* a plugin exists (via manifest), but nothing about *how* it works.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Mission Control (hermes-mission-control)               │
│                                                         │
│  ┌─────────────────┐     ┌──────────────────────────┐  │
│  │  Plugin Loader  │────▶│  server/plugins/*/       │  │
│  │  (discovery)    │     │  (internal plugins)      │  │
│  └─────────────────┘     └──────────────────────────┘  │
│           │                                             │
│           │     ┌──────────────────────────┐            │
│           └────▶│  ~/.hermes/mc-plugins/*/ │            │
│                 │  (external plugins)      │            │
│                 └──────────────────────────┘            │
│                                                         │
│  ┌─────────────────┐     ┌──────────────────────────┐  │
│  │  Plugin Registry│────▶│  src/plugins/*/          │  │
│  │  (frontend)     │     │  (UI components)         │  │
│  └─────────────────┘     └──────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

### Plugin sources (searched in order)

1. **Internal:** `server/plugins/<id>/` — bundled with MC
2. **External:** `~/.hermes/mc-plugins/<id>/` — installed via `git clone`

External plugins **override** internal plugins with the same ID.

---

## Plugin structure

Each plugin is a directory with this structure:

```
<plugin-id>/
├── manifest.json      # Plugin metadata + endpoint declarations
├── endpoints.py       # HTTP handler functions (request/response)
├── handlers.py        # Business logic (optional, imported by endpoints.py)
└── README.md          # Plugin documentation
```

### manifest.json

```json
{
  "id": "curate",
  "name": "Curate",
  "description": "Nightly brain candidate approval queue",
  "version": "1.0.0",
  "enabled": true,
  "routePath": "/curate",
  "navItem": {
    "to": "/curate",
    "label": "nav.curate",
    "icon": "ClipboardCheck",
    "order": 60
  },
  "endpoints": [
    {
      "method": "GET",
      "path": "/candidates",
      "handler": "listCandidates",
      "authRequired": true
    },
    {
      "method": "POST",
      "path": "/candidates/approve",
      "handler": "approveCandidate",
      "authRequired": true
    }
  ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | ✅ | Unique plugin identifier (kebab-case) |
| `name` | string | ✅ | Human-readable name |
| `description` | string | ✅ | Short description |
| `version` | string | ✅ | Semver version |
| `enabled` | boolean | ❌ | Default `true` |
| `routePath` | string | ❌ | Frontend route path (e.g. `/curate`) |
| `navItem` | object | ❌ | Sidebar navigation item |
| `navItem.to` | string | ✅ | Route path for nav |
| `navItem.label` | string | ✅ | Display label (i18n key or plain text) |
| `navItem.icon` | string | ✅ | Lucide icon name |
| `navItem.order` | number | ❌ | Sort order (lower = first, default 50) |
| `endpoints` | array | ❌ | HTTP endpoints registered by this plugin |
| `endpoints[].method` | string | ✅ | HTTP method: `GET`, `POST`, `PUT`, `DELETE` |
| `endpoints[].path` | string | ✅ | Path relative to `/api/local` |
| `endpoints[].handler` | string | ✅ | Function name in `endpoints.py` |
| `endpoints[].authRequired` | boolean | ❌ | Default `true` |

### endpoints.py

Exports handler functions declared in the manifest. Each handler receives:

```python
def myHandler(
    body: dict,           # Parsed JSON body (or {} for GET)
    params: dict,         # urllib.parse.parse_qs result (dict of lists)
    auth: Any,            # Auth context (reserved, currently None)
) -> dict:                # Response dict (JSON-serialized)
    ...
```

**Example:**

```python
# endpoints.py
from . import handlers  # or: import handlers (for external plugins)

class PluginError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message

def listCandidates(body, params, auth):
    status = (params.get("status") or [None])[0]
    vault = (params.get("vault") or [None])[0]
    cands = handlers.list_candidates(status=status, vault=vault)
    return {"candidates": cands, "count": len(cands)}

def approveCandidate(body, params, auth):
    cid = body.get("id", "")
    if not cid:
        raise PluginError(400, "bad_request", "Missing id.")
    cand = handlers.approve(cid)
    if not cand:
        raise PluginError(404, "not_found", f"Candidate {cid} not found.")
    return {"success": True, "candidate": cand}
```

**Rules:**
- Handler names must match `manifest.json` exactly
- Raise `PluginError(status_code, code, message)` for HTTP errors
- Return a dict for successful responses (200)
- Never crash — exceptions are caught and returned as 500

### handlers.py

Business logic — pure Python, no HTTP concerns. This module is optional but recommended for separation of concerns.

**Rules:**
- No HTTP-related code (no `self`, no `urllib`, no JSON parsing)
- Raise standard Python exceptions (`ValueError`, `FileNotFoundError`, etc.)
- `endpoints.py` translates these to HTTP errors

---

## Installing a plugin

### External plugin (recommended)

```bash
# Clone into the external plugins directory
git clone https://github.com/<org>/<plugin-repo>.git ~/.hermes/mc-plugins/<plugin-id>

# Restart Mission Control telemetry server
# The plugin is discovered automatically — no config needed
```

### Internal plugin (bundled with MC)

Place the plugin directory in `server/plugins/<id>/`. It will be bundled with MC's build.

---

## Creating a new plugin

### 1. Create the directory

```bash
mkdir -p ~/.hermes/mc-plugins/my-plugin
cd ~/.hermes/mc-plugins/my-plugin
```

### 2. Create `manifest.json`

```json
{
  "id": "my-plugin",
  "name": "My Plugin",
  "description": "What my plugin does",
  "version": "1.0.0",
  "routePath": "/my-plugin",
  "navItem": {
    "to": "/my-plugin",
    "label": "nav.myPlugin",
    "icon": "Puzzle",
    "order": 70
  },
  "endpoints": [
    {
      "method": "GET",
      "path": "/my-plugin/data",
      "handler": "getData"
    }
  ]
}
```

### 3. Create `endpoints.py`

```python
def getData(body, params, auth):
    return {"data": [1, 2, 3]}
```

### 4. Create `handlers.py` (optional)

```python
def get_data():
    return [1, 2, 3]
```

### 5. Register the frontend route (if needed)

In `src/plugins/<id>/manifest.ts`:

```ts
import type { MCPluginManifest } from '../../core/plugins/types';

export const myPluginManifest: MCPluginManifest = {
  id: 'my-plugin',
  name: 'My Plugin',
  description: 'What my plugin does',
  version: '1.0.0',
  routePath: '/my-plugin',
  navItem: { to: '/my-plugin', label: 'nav.myPlugin', icon: 'Puzzle', order: 70 },
  endpoints: [
    { method: 'GET', path: '/my-plugin/data', handler: 'getData' },
  ],
};
```

Then register in `src/App.tsx`:

```ts
import { MyPluginPlugin } from './plugins/my-plugin/route';
import { myPluginManifest } from './plugins/my-plugin/manifest';

registry.load([
  // ... existing plugins ...
  {
    manifest: myPluginManifest,
    component: MyPluginPlugin,
    loadRoute: () => Promise.resolve({ default: MyPluginPlugin }),
  },
]);
```

### 6. Restart MC

The plugin is discovered at startup. Restart the telemetry server to load it.

---

## Frontend integration

### Fetch API helpers

Use the plugin registry to resolve endpoint URLs instead of hardcoding paths:

```ts
// ❌ Don't hardcode
const response = await fetch(apiUrl('/candidates'), {...});

// ✅ Use the registry
const url = registry.resolveEndpointUrl('curate', 'listCandidates');
const response = await fetch(apiUrl(url), {...});
```

### Type-safe fetch helper

```ts
// In your plugin's endpoints.ts
import { registry } from '../../core/registry';  // singleton instance

export async function loadMyData(accessToken?: string) {
  const path = registry.resolveEndpointUrl('my-plugin', 'getData');
  if (!path) throw new Error('Plugin endpoint not found');
  const { payload } = await maybeFetchLocalJson<MyDataType>(path, accessToken);
  return payload;
}
```

---

## Backend dispatch flow

```
HTTP Request
    │
    ▼
local_telemetry_server.py (do_GET / do_POST / ...)
    │
    ├─ Known route? → Handle directly
    │
    └─ /api/local/* unknown? → dispatch_plugin_request()
                                │
                                ▼
                          PluginLoader.resolve(method, path)
                                │
                                ├─ Found? → handler_fn(body, params, auth)
                                │           │
                                │           ▼
                                │         Response dict → JSON 200
                                │
                                └─ Not found? → 404
```

---

## Error handling

| Error type | HTTP status | When |
|------------|-------------|------|
| `PluginError(400, ...)` | 400 | Bad request (missing fields, invalid JSON) |
| `PluginError(403, ...)` | 403 | Forbidden (vault not curable, etc.) |
| `PluginError(404, ...)` | 404 | Not found (resource doesn't exist) |
| `PluginError(409, ...)` | 409 | Conflict (state transition not valid) |
| Unhandled exception | 500 | Internal server error (logged) |

---

## Plugin lifecycle

1. **Discovery** — At startup, the loader scans `server/plugins/*/` and `~/.hermes/mc-plugins/*/`
2. **Registration** — Each plugin's `manifest.json` is loaded, endpoints are registered
3. **Dispatch** — Incoming requests are matched against registered endpoints
4. **Teardown** — Plugins are not unloaded at runtime (restart to reload)

---

## FAQ

**Q: Can I override an internal plugin with an external one?**
A: Yes. External plugins take precedence over internal plugins with the same ID.

**Q: Do I need to set environment variables to enable a plugin?**
A: No. If the plugin is installed (directory exists with `manifest.json`), it's active.

**Q: Can a plugin have no endpoints (UI-only)?**
A: Yes. Omit `endpoints` from the manifest. The plugin will appear in the nav but register no backend routes.

**Q: Can a plugin have no UI (backend-only)?**
A: Yes. Omit `routePath` and `navItem`. The plugin's endpoints will be available but no sidebar entry.

**Q: How do I debug a plugin?**
A: Check the telemetry server logs. Failed plugin loads are logged with the error. Use `curl` to test endpoints directly.

**Q: Can plugins depend on each other?**
A: Not yet. Each plugin is independent. Future versions may support `depends_on` in the manifest.

---

## Example: Curate plugin

See [albidev/mc-curate-plugin](https://github.com/albidev/mc-curate-plugin) (private) for a complete example.

```
curate/
├── manifest.json      # 4 endpoints: list, list-vaults, approve, reject
├── endpoints.py       # HTTP translation layer with PluginError
├── handlers.py        # Business logic (candidate CRUD, quarantine, promote)
└── README.md
```
