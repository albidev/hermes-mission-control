import type { MCPluginManifest } from '../../core/plugins/types';

export const curateManifest: MCPluginManifest = {
  id: 'curate',
  name: 'Curate',
  description: 'Nightly brain candidate approval queue',
  version: '1.0.0',
  enabled: true,
  navItem: {
    to: '/curate',
    label: 'nav.curate',
    icon: 'ClipboardCheck',
    showWhen: (ctx) => ctx.snapshot.candidatesEnabled,
    order: 60,
  },
  routePath: '/curate',
  lazyRoute: true,
  permissions: [],
};
