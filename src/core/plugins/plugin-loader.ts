import type { MCPluginManifest } from './types';
import type { InternalPlugin } from './registry';

/**
 * Plugin manifest descriptor — known plugins that MC attempts to load.
 * Each entry includes the manifest metadata for UI registration.
 */
interface KnownPluginDescriptor {
  id: string;
  fallbackManifest: MCPluginManifest;
}

/**
 * Known plugins — MC attempts to load these at startup.
 * If a plugin is not installed (symlink missing), the import fails silently
 * and the plugin is not registered.
 */
const KNOWN_PLUGINS: KnownPluginDescriptor[] = [
  // Curate plugin
  {
    id: 'curate',
    fallbackManifest: {
      id: 'curate',
      name: 'Curate',
      description: 'Nightly brain candidate approval queue',
      version: '1.0.0',
      enabled: true,
      routePath: '/curate',
      lazyRoute: true,
      navItem: {
        to: '/curate',
        label: 'nav.curate',
        icon: 'ClipboardCheck',
        order: 60,
      },
      endpoints: [
        { method: 'GET', path: '/candidates', handler: 'listCandidates', authRequired: true },
        { method: 'GET', path: '/candidates/vaults', handler: 'listVaults', authRequired: true },
        { method: 'POST', path: '/candidates/approve', handler: 'approveCandidate', authRequired: true },
        { method: 'POST', path: '/candidates/reject', handler: 'rejectCandidate', authRequired: true },
      ],
    },
  },
];

/**
 * Try to load a plugin's UI module.
 * Returns null if the plugin is not installed (symlink missing or import fails).
 */
async function tryLoadPluginUI(
  descriptor: KnownPluginDescriptor
): Promise<InternalPlugin | null> {
  try {
    // Attempt dynamic import — works if symlink exists in src/plugins/<id>/
    const routeModule = await import(`../plugins/${descriptor.id}/route.ts`);
    const manifestModule = await import(`../plugins/${descriptor.id}/manifest.ts`);

    const component =
      routeModule[`${descriptor.id}Plugin`] ||
      routeModule.default ||
      null;

    const manifest: MCPluginManifest =
      manifestModule[`${descriptor.id}Manifest`] ||
      manifestModule.default ||
      descriptor.fallbackManifest;

    if (!component) {
      console.warn(`Plugin ${descriptor.id}: no UI component found`);
      return null;
    }

    return {
      manifest,
      component,
      loadRoute: async () => {
        const mod = await import(`../plugins/${descriptor.id}/route.ts`);
        return { default: mod[`${descriptor.id}Plugin`] ?? mod.default };
      },
    };
  } catch {
    // Plugin not installed or failed to load — silent skip
    return null;
  }
}

/**
 * Load all known plugins at app startup.
 * Returns only plugins that are actually installed.
 */
export async function loadPlugins(): Promise<InternalPlugin[]> {
  const loaded: InternalPlugin[] = [];
  for (const descriptor of KNOWN_PLUGINS) {
    const plugin = await tryLoadPluginUI(descriptor);
    if (plugin) {
      loaded.push(plugin);
    }
  }
  return loaded;
}

export type { InternalPlugin } from './registry';
export type { KnownPluginDescriptor };
