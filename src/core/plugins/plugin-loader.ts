import React from 'react';
import type { MCPluginManifest } from './types';
import type { InternalPlugin } from './registry';

/** External plugin UI modules linked into src/plugins/<id>/ by setup-plugins.sh. */
const routeModules = import.meta.glob('../../plugins/*/route.ts', { eager: true });
const manifestModules = import.meta.glob('../../plugins/*/manifest.ts', { eager: true });
const attentionModules = import.meta.glob('../../plugins/*/attention.tsx', { eager: true });

function pluginIdFromPath(path: string): string | null {
  const match = path.match(/\/plugins\/([^/]+)\/(?:route|manifest|attention)\.(?:ts|tsx)$/);
  return match?.[1] ?? null;
}

function moduleExport(module: Record<string, unknown>, id: string, suffix: string): unknown {
  const pascalId = id.charAt(0).toUpperCase() + id.slice(1);
  return module[`${id}${suffix}`] ?? module[`${pascalId}${suffix}`] ?? module.default;
}

export function loadPlugins(): InternalPlugin[] {
  const loaded = new Map<string, InternalPlugin>();
  for (const [routePath, routeValue] of Object.entries(routeModules)) {
    const id = pluginIdFromPath(routePath);
    if (!id) continue;
    const manifestPath = Object.keys(manifestModules).find((path) => pluginIdFromPath(path) === id);
    if (!manifestPath) continue;
    const manifest = moduleExport(manifestModules[manifestPath] as Record<string, unknown>, id, 'Manifest') as MCPluginManifest | undefined;
    const component = moduleExport(routeValue as Record<string, unknown>, id, 'Plugin') as React.ComponentType<any> | undefined;
    if (!manifest || !component) continue;
    const attentionPath = Object.keys(attentionModules).find((path) => pluginIdFromPath(path) === id);
    const attention = attentionPath
      ? moduleExport(attentionModules[attentionPath] as Record<string, unknown>, id, 'Attention') as React.ComponentType<any> | undefined
      : undefined;
    loaded.set(id, { manifest, component, attention, loadRoute: async () => ({ default: component }) });
  }
  return [...loaded.values()].filter((plugin) => plugin.manifest.enabled !== false);
}

export type { InternalPlugin } from './registry';
