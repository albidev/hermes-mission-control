# Plugin System

Mission Control supports **internal plugins**: self-contained feature modules that register their own route, sidebar entry, and (in the future) backend endpoints — without touching core navigation or routing code.

The first plugin shipped with this system is **Curate** (nightly brain candidate approval), which lives entirely under `src/plugins/curate/`.

## Architecture

```
src/
├── core/plugins/
│   ├── types.ts        # Plugin contract: manifest, nav item, route, init
│   └── registry.ts     # PluginRegistry: load() → nav items + routes
├── plugins/
│   └── <plugin-id>/
│       ├── manifest.ts # Contract: id, name, navItem, routePath, icon
│       ├── route.ts    # Re-export of the route component
│       └── <UI files>  # Self-contained component(s) and assets
└── lib/
    └── icons.ts        # resolveIcon(): string icon name → Lucide component
```

### Core types (`src/core/plugins/types.ts`)

| Type | Purpose |
|------|---------|
| `MCPluginManifest` | Plugin identity: `id`, `name`, `description`, `version`, `enabled`, `navItem`, `routePath`, `permissions` |
| `MCPluginNavItem` | Sidebar entry: `to`, `label` (i18n key or plain text), `icon` (Lucide name), optional `showWhen` + `order` |
| `MCPluginRoute` | Route registration: relative `path` + `element` (React element, React Router v6) |
| `MCPluginInit` | Optional init hook for dynamic registration (reserved for external plugins) |

### Registry (`src/core/plugins/registry.ts`)

`PluginRegistry` is initialized **synchronously at module level** in `App.tsx` so plugin routes exist from the very first render (no async gap, no redirect to `/`).

- `load(plugins)` — registers internal plugins, derives nav items and routes
- `getNavItems()` — sorted sidebar entries (used by `MissionControlShell`)
- `getRoutes()` — route elements (used by `App.tsx`)

Route paths are **relative** (no leading slash) to match React Router v6 nested-route rules. `element` must be a **React element** (`React.createElement(...)`), not a component function.

### Sidebar

Core navigation items are rendered first, followed by a **PLUGINS** section (eyebrow-style label) listing plugin nav items. The section only appears when at least one plugin is registered. Labels are resolved through i18n when they look like keys (contain a `.`).

## Adding a plugin

1. Create `src/plugins/<plugin-id>/` with:
   - `manifest.ts` — export `const <id>Manifest: MCPluginManifest`
   - `route.ts` — export the route component (or a lazy loader)
   - the UI component(s) — keep them **self-contained** inside the plugin folder
2. Register it in `src/App.tsx`:

```ts
import { MyPlugin } from './plugins/myplugin/route';
import { myPluginManifest } from './plugins/myplugin/manifest';

registry.load([
  { manifest: curateManifest, component: CuratePlugin, loadRoute: () => Promise.resolve({ default: CuratePlugin }) },
  { manifest: myPluginManifest, component: MyPlugin, loadRoute: () => Promise.resolve({ default: MyPlugin }) },
]);
```

3. Build and verify: `node ./node_modules/vite/bin/vite.js build`

## Design notes

- **Self-contained**: a plugin owns its full UI. No core file should import from `src/plugins/<id>/` except the registration point in `App.tsx`.
- **No submodules**: internal plugins are plain directories in the repo. External plugins (runtime-loaded from a registry) are a future extension — the `MCPluginInit` hook and `endpoints` manifest field are reserved for that.
- **`showWhen`**: currently evaluated against a hardcoded context (`candidatesEnabled: true`). Wiring it to the live store snapshot is a known follow-up.
- **Overview Attention**: the Overview "Attention needed" card reads candidate data directly from the telemetry API and links to the plugin's route. Associating that card with plugin lifecycle (hide when the plugin is disabled) is a known follow-up.

## Pitfalls

- **JSDoc globs**: never write `src/plugins/*/manifest.ts` inside a block comment — esbuild treats `*/` as the comment terminator and the rest of the line becomes code (500 on Vite).
- **Import paths**: files inside `src/plugins/<id>/` are one level deeper than `src/routes/` — use `../../lib/...`, `../../components/...`.
- **React Router v6**: `Route element` requires a React element, not a component function. Use `React.createElement(Component)` or JSX.
- **i18n labels**: plugin nav labels are i18n keys (`nav.curate`); the shell translates them. Plain-text labels are also supported.
