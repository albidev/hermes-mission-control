import { lazy } from 'react';
import type { CanvasAddonDescriptor } from './types';
import type { CanvasAddonId } from './types';

export const CANVAS_ADDONS: CanvasAddonDescriptor[] = [
  {
    id: 'tldraw',
    label: 'TLDraw',
    description: 'Whiteboard collaborativa con bridge agent',
    icon: '◉',
    component: lazy(() => import('./tldraw/TLDrawAddon').then(({ TLDrawAddon }) => ({ default: TLDrawAddon }))),
    lifecycleTimeout: 15000,
    supportsPersistence: true,
    supportsCommands: true,
  },
];

export const ADDON_INDEX: Record<CanvasAddonId, CanvasAddonDescriptor> = Object.fromEntries(
  CANVAS_ADDONS.map(a => [a.id, a]),
) as Record<CanvasAddonId, CanvasAddonDescriptor>;
