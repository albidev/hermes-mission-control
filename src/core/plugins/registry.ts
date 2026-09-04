import React from 'react';
import type { MCPluginManifest, MCPluginNavItem, MCPluginRoute } from './types';

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
 * Discovers plugins in src/plugins and builds routes + nav items.
 */
export class PluginRegistry {
  private plugins: InternalPlugin[] = [];
  private navItems: MCPluginNavItem[] = [];
  private routes: MCPluginRoute[] = [];

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
}
