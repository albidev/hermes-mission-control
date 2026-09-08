import React from 'react';
import type { MCPluginManifest } from './types';
import type { InternalPlugin } from './registry';

/**
 * External plugin UI modules are linked into src/plugins/<id>/ by
 * scripts/setup-plugins.sh. The glob is intentionally generic: MC does not
 * contain a list of plugin IDs and therefore does not know Curate or any
 * other plugin before installation.
 */
const routeModules = import.meta.glob('../../plugins/*/route.ts', { eager: true });
const manifestModules = import.meta.glob('../../plugins/*/manifest.ts', { eager: true });

function pluginIdFromPath(path: string): string | null {
  const match = path.match(/\/plugins\/([^/]+)\/(?:route|manifest)\.ts$/);
  return match?.[1] ?? null;
}

function moduleExport(module: Record<string, unknown>, id: string, suffix: string): unknown {
  return module[`${id}${suffix}`] ?? module.default;
}

/**
 * Load every installed plugin UI discovered by Vite's generic glob.
 * Missing/invalid plugin modules are skipped without affecting MC startup.
 */
export function loadPlugins(): InternalPlugin[] {
  const loaded = new Map<string, InternalPlugin>();

  for (const [routePath, routeValue] of Object.entries(routeModules)) {
    const id = pluginIdFromPath(routePath);
    if (!id) continue;
    const routeModule = routeValue as Record<string, unknown>;
    const manifestPath = Object.keys(manifestModules).find((path) => pluginIdFromPath(path) === id);
    if (!manifestPath) continue;

    const manifest = moduleExport(manifestModules[manifestPath] as Record<string, unknown>, id, 'Manifest') as MCPluginManifest | undefined;
    const component = moduleExport(routeModule, id, 'Plugin') as React.ComponentType<any> | undefined;
    if (!manifest || !component) continue;

    loaded.set(id, {
      manifest,
      component,
      loadRoute: async () => ({ default: component }),
    });
  }

  return [...loaded.values()].filter((plugin) => plugin.manifest.enabled !== false);
}

export type { InternalPlugin } from './registry';
