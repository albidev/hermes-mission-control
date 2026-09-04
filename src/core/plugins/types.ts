/**
 * Mission Control Plugin System — Core Types
 * 
 * Internal plugins live in src/plugins/<name>/
 * External plugins are loaded at runtime from ~/.hermes/mc-plugins/
 */

export interface MCPluginManifest {
  /** Unique plugin identifier (kebab-case, e.g. 'curate', 'kanban') */
  id: string;
  /** Human-readable name */
  name: string;
  /** Short description for UI */
  description: string;
  /** Semver version */
  version: string;
  /** Minimum MC core version required */
  minCoreVersion?: string;
  /** Plugin author */
  author?: string;
  /** Whether this plugin is enabled by default */
  enabled?: boolean;
  /** Navigation item for sidebar (optional) */
  navItem?: MCPluginNavItem;
  /** Route path (e.g. '/curate') — auto-prefixed if not absolute */
  routePath?: string;
  /** Whether to lazy-load the route component */
  lazyRoute?: boolean;
  /** Required permissions/capabilities */
  permissions?: string[];
  /** Backend endpoints this plugin registers on telemetry server */
  endpoints?: MCPluginEndpoint[];
  /** Plugin-specific configuration schema (for future settings UI) */
  configSchema?: Record<string, unknown>;
}

export interface MCPluginNavItem {
  /** Route path (e.g. '/curate') */
  to: string;
  /** Nav label (i18n key or plain text) */
  label: string;
  /** Lucide icon name (as string, resolved at runtime) */
  icon: string;
  /** Optional condition to show nav item (evaluated at runtime) */
  showWhen?: (context: MCPluginNavContext) => boolean;
  /** Nav item order (lower = first) */
  order?: number;
}

export interface MCPluginNavContext {
  /** Current snapshot from mission-control-store */
  snapshot: {
    candidatesEnabled: boolean;
    activeModel: string;
    // extend as needed
  };
  /** Auth state */
  authRequired: boolean;
  /** Stored token */
  storedToken: string | undefined;
}

export interface MCPluginEndpoint {
  /** HTTP method */
  method: 'GET' | 'POST' | 'PUT' | 'DELETE';
  /** Path relative to /api/local (e.g. '/plugins/curate/candidates') */
  path: string;
  /** Handler function name exported from plugin's endpoints module */
  handler: string;
  /** Whether auth is required */
  authRequired?: boolean;
}

export interface MCPluginRoute {
  /** Route path (e.g. '/curate') — relative to parent Route (no leading slash) */
  path: string;
  /** React element to render (React Router v6 requires an element, not a component fn) */
  element: React.ReactElement;
  /** Whether this is the index route */
  index?: boolean;
}

/** Plugin registration result */
export interface MCPluginRegistration {
  manifest: MCPluginManifest;
  routes: MCPluginRoute[];
  endpoints: MCPluginEndpoint[];
  navItems: MCPluginNavItem[];
}

/** Context passed to plugin init (for future extensibility) */
export interface MCPluginInitContext {
  /** Register additional routes dynamically */
  registerRoute: (route: MCPluginRoute) => void;
  /** Register nav items dynamically */
  registerNavItem: (item: MCPluginNavItem) => void;
  /** Access to core services */
  services: {
    i18n: { t: (key: string, params?: Record<string, string | number>) => string };
    // extend as needed
  };
}

export type MCPluginInit = (context: MCPluginInitContext) => void | Promise<void>;