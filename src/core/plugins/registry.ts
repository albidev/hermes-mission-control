import React from 'react';
import type { MCPluginAttentionContributor, MCPluginManifest, MCPluginNavItem, MCPluginRoute, MCPluginEndpoint } from './types';


export interface InternalPlugin {
  manifest: MCPluginManifest;
  loadRoute: () => Promise<{ default: React.ComponentType<any> }>;
  component?: React.ComponentType<any>;
  attention?: React.ComponentType<any>;
}

function isNavItem(item: MCPluginNavItem | null | undefined): item is MCPluginNavItem {
  return item != null;
}

export class PluginRegistry {
  private plugins: InternalPlugin[] = [];
  private navItems: MCPluginNavItem[] = [];
  private routes: MCPluginRoute[] = [];
  private endpoints = new Map<string, MCPluginEndpoint[]>();
  private loaded = false;

  load(plugins: InternalPlugin[]): void {
    this.plugins = plugins;
    this.buildNavItems();
    this.buildRoutes();
    this.registerEndpoints();
  }

  private buildNavItems(): void {
    const ctx = {
      snapshot: { candidatesEnabled: true, activeModel: '' },
      authRequired: false,
      storedToken: '',
    } as any;
    this.navItems = this.plugins
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
  }

  private buildRoutes(): void {
    this.routes = this.plugins.map((p) => {
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

  private registerEndpoints(): void {
    for (const p of this.plugins) {
      if (p.manifest.endpoints) {
        this.endpoints.set(p.manifest.id, p.manifest.endpoints);
      }
    }
  }

  getNavItems(): MCPluginNavItem[] {
    return this.navItems;
  }

  getAttentionContributors(): MCPluginAttentionContributor[] {
    const contributors: MCPluginAttentionContributor[] = [];
    for (const plugin of this.plugins) {
      if (plugin.manifest.surfaces?.attention?.enabled === false) continue;
      const component = (plugin as InternalPlugin & { attention?: React.ComponentType<any> }).attention;
      if (component) contributors.push({ id: plugin.manifest.id, order: plugin.manifest.surfaces?.attention?.order ?? 50, component });
    }
    return contributors.sort((a, b) => (a.order ?? 50) - (b.order ?? 50));
  }

  getRoutes(): MCPluginRoute[] {
    return this.routes;
  }

  getPlugin(id: string): InternalPlugin | undefined {
    return this.plugins.find((p) => p.manifest.id === id);
  }

  isEnabled(id: string): boolean {
    const p = this.getPlugin(id);
    return p?.manifest.enabled ?? false;
  }

  getEndpoints(pluginId: string): MCPluginEndpoint[] {
    return this.endpoints.get(pluginId) ?? [];
  }

  resolveEndpointUrl(pluginId: string, handlerName: string): string | null {
    const endpoints = this.endpoints.get(pluginId);
    if (!endpoints) return null;
    const ep = endpoints.find((e) => e.handler === handlerName);
    return ep?.path ?? null;
  }

  getAllEndpoints(): Array<MCPluginEndpoint & { pluginId: string }> {
    const result: Array<MCPluginEndpoint & { pluginId: string }> = [];
    for (const [pluginId, endpoints] of this.endpoints) {
      for (const ep of endpoints) {
        result.push({ ...ep, pluginId });
      }
    }
    return result;
  }

  isEmpty(): boolean {
    return this.plugins.length === 0;
  }
}
