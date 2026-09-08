# Mission Control plugins

Mission Control supports **external plugins**. A plugin is a separate repository containing its backend, UI, manifest, and tests. Mission Control discovers installed plugins at runtime; it does not contain plugin implementation code or a hardcoded list of plugin IDs.

> **Boundary:** MC provides the host runtime. The plugin owns its feature. If a plugin is not installed, MC must still build, start, and work normally.

## What MC provides

MC provides only the plugin host contract:

- backend discovery and endpoint dispatch in `server/plugins/loader.py`;
- frontend discovery through a generic `import.meta.glob` in `src/core/plugins/plugin-loader.ts`;
- route and navigation registration through `PluginRegistry`;
- the `/api/local/plugins` discovery endpoint;
- `scripts/setup-plugins.sh`, which creates local UI symlinks for installed plugins;
- authentication, the `/api/local` transport, shared UI primitives, and the telemetry lifecycle.

MC must not import a plugin component, import a plugin business module, or add plugin-specific routes to `local_telemetry_server.py`.

## Installing an external plugin

External plugins are installed under `~/.hermes/mc-plugins/<plugin-id>/`.

```bash
mkdir -p ~/.hermes/mc-plugins
git clone https://github.com/<owner>/<plugin-repository>.git \
  ~/.hermes/mc-plugins/<plugin-id>

cd /path/to/hermes-mission-control
bash scripts/setup-plugins.sh
```

For a private repository, authenticate GitHub first (for example with `gh auth login`, SSH, or an approved credential helper). Do not put tokens in the clone URL or commit them to a repository.

Restart both processes after installing or updating a plugin:

```bash
# restart the telemetry sidecar
# restart Vite in development, or rebuild/restart the deployed frontend
```

The backend loader reads external plugin files directly from `~/.hermes/mc-plugins/`. The setup script creates a local symlink from `src/plugins/<plugin-id>` to the plugin's `ui/` directory so Vite can bundle the plugin UI. The symlink is local-only and must not be committed.

To remove a plugin:

```bash
rm -rf ~/.hermes/mc-plugins/<plugin-id>
bash scripts/setup-plugins.sh
```

Use the normal backup/approval policy before destructive operations. `rm -rf` is shown only as the uninstall operation; verify the plugin ID first.

## Plugin repository contract

A plugin repository should have this shape:

```text
mc-example-plugin/
├── manifest.json          # required: backend manifest
├── endpoints.py           # required for backend endpoints
├── handlers.py            # optional: business logic
├── ui/                    # optional: frontend integration
│   ├── route.ts           # exports <id>Plugin or default component
│   ├── manifest.ts        # exports <id>Manifest or default manifest
│   ├── types.ts           # plugin-local types
│   └── ...
├── tests/                 # plugin-owned tests
└── README.md              # installation and feature documentation
```

A backend-only plugin may omit `ui/`. A UI-only plugin may omit `endpoints`.

### `manifest.json`

The backend manifest is the runtime contract between the plugin and MC:

```json
{
  "id": "example",
  "name": "Example",
  "description": "Example Mission Control integration",
  "version": "1.0.0",
  "enabled": true,
  "routePath": "/example",
  "navItem": {
    "to": "/example",
    "label": "Example",
    "icon": "Puzzle",
    "order": 70
  },
  "endpoints": [
    {
      "method": "GET",
      "path": "/example/items",
      "handler": "listItems",
      "authRequired": true
    }
  ]
}
```

| Field | Required | Meaning |
|---|---:|---|
| `id` | yes | Unique kebab-case plugin ID; must match the install directory. |
| `name` | yes | Human-readable name. |
| `description` | yes | Short feature description. |
| `version` | yes | Plugin version, preferably SemVer. |
| `enabled` | no | Defaults to enabled when omitted. |
| `routePath` | no | Frontend route, for example `/example`. |
| `navItem` | no | Sidebar entry. Omit for a hidden/backend-only plugin. |
| `endpoints` | no | HTTP endpoint declarations. |

Endpoint paths are relative to `/api/local`. For example, `"/example/items"` is served at `/api/local/example/items`. The endpoint `handler` must exactly match an exported function in `endpoints.py`.

External plugins take precedence over an internal plugin with the same ID. Internal plugins are supported for host-owned integrations, but new feature work should use a separate repository.

### Backend handlers

`endpoints.py` is the HTTP adapter. A handler receives the parsed JSON body, query parameters, and an auth context, and returns a JSON-serializable dictionary:

```python
from __future__ import annotations

import handlers


class PluginError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def listItems(body: dict, params: dict, auth: object = None) -> dict:
    limit = int((params.get("limit") or ["50"])[0])
    return {"items": handlers.list_items(limit=limit)}
```

Rules:

- keep HTTP parsing and response translation in `endpoints.py`;
- keep business logic in `handlers.py` or other plugin-owned modules;
- return dictionaries/lists that can be JSON-serialized;
- raise an exception carrying `status_code`, `code`, and `message` for a controlled HTTP error;
- do not import or modify Hermes core files from MC plugin code unless the plugin's documented contract explicitly requires it;
- do not write outside the plugin's documented data roots;
- never log access tokens or sensitive payloads.

The loader catches handler exceptions and returns an error response. Plugins should still validate inputs and provide useful, non-sensitive error messages.

## Frontend contract

If the plugin has a UI, `ui/route.ts` must export either `<id>Plugin` or a default React component. For `id: "example"`:

```ts
export { ExampleRoute as ExamplePlugin } from './ExampleRoute';
```

`ui/manifest.ts` must export either `exampleManifest` or a default manifest. It should mirror the backend metadata and endpoint declarations:

```ts
import type { MCPluginManifest } from './types';

export const exampleManifest: MCPluginManifest = {
  id: 'example',
  name: 'Example',
  description: 'Example Mission Control integration',
  version: '1.0.0',
  routePath: '/example',
  navItem: {
    to: '/example',
    label: 'Example',
    icon: 'Puzzle',
    order: 70,
  },
  endpoints: [
    { method: 'GET', path: '/example/items', handler: 'listItems' },
  ],
};
```

The plugin UI should use the host's current-origin API path (`/api/local/...`) and the stored Mission Control bearer token. It must handle the plugin backend being unavailable without crashing the host UI.

Do not add plugin-specific imports to `src/App.tsx`, `MissionControlShell.tsx`, or `hermes-api.ts`. The generic loader discovers UI modules and registers their routes, nav items, and endpoint metadata.

## Lifecycle

1. **Install:** clone the plugin repository into `~/.hermes/mc-plugins/<id>/`.
2. **Link UI:** run `scripts/setup-plugins.sh`.
3. **Load backend:** restart the telemetry server; `PluginLoader` scans the plugin directory and imports its manifest/handlers.
4. **Load frontend:** restart Vite or rebuild; the generic glob discovers the local UI symlink.
5. **Register:** MC adds the plugin's routes, navigation item, and endpoint metadata.
6. **Dispatch:** requests to `/api/local/...` are resolved from the backend manifest.
7. **Update:** pull the plugin repository, rerun setup, and restart both host processes.
8. **Uninstall:** remove the plugin directory, rerun setup, and restart.

There is no plugin enable environment variable. Presence of a valid plugin directory and manifest is the activation mechanism. A plugin may expose its own configuration if it needs a user-controlled enabled/disabled state; that state belongs to the plugin, not to a Curate-specific MC flag.

## Testing requirements for plugin authors

A plugin repository should test at least:

- manifest validation and endpoint/handler alignment;
- unauthenticated and authenticated requests;
- malformed input and controlled error responses;
- backend behavior when its data directory is absent or empty;
- UI rendering with an empty response, loading state, and backend failure;
- mobile layout for every plugin route;
- install/update/uninstall instructions from a clean checkout.

From the MC repository, verify the host without any plugin installed:

```bash
rm -rf ~/.hermes/mc-plugins/<plugin-id>
bash scripts/setup-plugins.sh
pnpm install --frozen-lockfile
pnpm build
pnpm test:mobile-route-layout
python3 -m unittest discover -s tests -p 'test_*.py'
```

Then install the plugin and verify the integration separately. The host CI must not depend on a private plugin repository being available.

## Curate reference implementation

Curate is the first external plugin:

- repository: [`albidev/mc-curate-plugin`](https://github.com/albidev/mc-curate-plugin) (private);
- install path: `~/.hermes/mc-plugins/curate/`;
- backend: candidate listing, vault listing, approve, and reject endpoints;
- UI: candidate review route under the plugin's `ui/` directory.

Curate is an example of the contract, not a special case in MC. New plugins must not copy Curate-specific code into Mission Control.
