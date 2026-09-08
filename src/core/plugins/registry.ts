import React from 'react';
import type { MCPluginManifest, MCPluginNavItem, MCPluginRoute, MCPluginEndpoint } from './types';

/**
 * Internal plugin — manifest + route info.
 * Used by PluginRegistry to discover and mount routes.
 */
export interface InternalPlugin {
  manifest: MCPluginManifest;
  /** Lazy-import function for the route component */
  loadRoute: () => Promise<{ default: React.ComponentType<any> }>;
  /** Sync component for non-lazy routes */
  component?: React.ComponentType<any>;
}

function isNavItem(item: MCPluginNavItem | null | undefined): item is MCPluginNavItem {
  return item != null;
}

/**
 * Registry for internal plugins.
 * Discovers plugins in src/plugins and builds routes + nav items + endpoint resolution.
 */
export class PluginRegistry {
  private plugins: InternalPlugin[] = [];
  private navItems: MCPluginNavItem[] = [];
  private routes: MCPluginRoute[] = [];
  private endpoints = new Map<string, MCPluginEndpoint[]>();  // pluginId -> endpoints[]

  /**
   * Load all internal plugins from manifest files.
   * Called once at app boot.
   */
  load(plugins: InternalPlugin[]): void {
    this.plugins = plugins;

    // Build nav items from manifests (filter by showWhen if present)
    const ctx = {
      snapshot: { candidatesEnabled: true, activeModel: '' },
      authRequired: false,
      storedToken: '',
    } as any;
    this.navItems = plugins
      .map((p) => {
        const nav = p.manifest.navItem;
        if (!nav) return null;
        return {
          ...nav,
          to: nav.to ?? p.manifest.routePath ?? `/${p.manifest.id}`,
        };
      })
      .filter(isNavItem)
      .sort((a, b) => (a.order ?? 50) - (b.order ?? 50))
      .filter((item) => !item.showWhen || item.showWhen(ctx));

    // Build routes — use sync component when available, else lazy.
    // Paths are RELATIVE (no leading slash) to match React Router v6
    // nested-route rules (default routes use 'sessions', 'kanban', etc.).
    // element must be a ReactElement (React Router v6), not a component fn.
    this.routes = plugins.map((p) => {
      const rawPath = p.manifest.routePath ?? `/${p.manifest.id}`;
      const path = rawPath.startsWith('/') ? rawPath.slice(1) : rawPath;
      return {
        path,
        element: p.component
          ? React.createElement(p.component)
          : React.createElement(React.lazy(p.loadRoute)),
        index: false,
      };
    });

    // Register endpoints from manifests
    for (const p of plugins) {
      if (p.manifest.endpoints) {
        this.endpoints.set(p.manifest.id, p.manifest.endpoints);
      }
    }
  }

  /** Get sorted nav items for plugin-enabled sidebar entries */
  getNavItems(): MCPluginNavItem[] {
    return this.navItems;
  }

  /** Get registered lazy routes (for App.tsx Routes) */
  getRoutes(): MCPluginRoute[] {
    return this.routes;
  }

  /** Get plugin by id */
  getPlugin(id: string): InternalPlugin | undefined {
    return this.plugins.find((p) => p.manifest.id === id);
  }

  /** Check if a plugin is enabled */
  isEnabled(id: string): boolean {
    const p = this.getPlugin(id);
    return p?.manifest.enabled ?? false;
  }

  /**
   * Get all registered endpoints for a plugin.
   * Used by fetch helpers to resolve URLs.
   */
  getEndpoints(pluginId: string): MCPluginEndpoint[] {
    return this.endpoints.get(pluginId) ?? [];
  }

  /**
   * Resolve the URL for a specific plugin endpoint.
   * Returns the path relative to /api/local (e.g. '/candidates').
   * Returns null if the endpoint is not found.
   *
   * Usage:
   *   const url = registry.resolveEndpointUrl('curate', 'listCandidates');
   *   // => '/candidates'
   *   const fullUrl = apiUrl(url);  // => '/api/local/candidates'
   */
  resolveEndpointUrl(pluginId: string, handlerName: string): string | null {
    const endpoints = this.endpoints.get(pluginId);
    if (!endpoints) return null;
    const ep = endpoints.find((e) => e.handler === handlerName);
    return ep?.path ?? null;
  }

  /**
   * Get all endpoints across all plugins.
   * Useful for debugging/health checks.
   */
  getAllEndpoints(): Array<MCPluginEndpoint & { pluginId: string }> {
    const result: Array<MCPluginEndpoint & { pluginId: string }> = [];
    for (const [pluginId, endpoints] of this.endpoints) {
      for (const ep of endpoints) {
        result.push({ ...ep, pluginId });
      }
    }
    return result;
  }
}
