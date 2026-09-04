import type { MCPluginManifest, MCPluginInit } from '../../core/plugins/types';

export const curateManifest: MCPluginManifest = {
  id: 'curate',
  name: 'Curate',
  description: 'Nightly brain candidate approval queue',
  version: '1.0.0',
  enabled: true,
  navItem: {
    label: 'nav.curate',
    icon: 'ClipboardCheck',
    showWhen: (ctx) => ctx.snapshot.candidatesEnabled,
    order: 60,
  },
  routePath: '/curate',
  lazyRoute: true,
  permissions: [],
};

export const curateInit: MCPluginInit = (ctx) => {
  // Dynamic nav/route registration (future: used for external plugins)
  // For internal plugins, registry reads manifest directly.
  _registerInternal();

  function _registerInternal() {
    // Reserved for dynamic registration if needed.
    // Internal plugins register via manifest in registry.ts
  }
};

// Export for registry discovery
export type { MCPluginManifest as CurateManifest };
