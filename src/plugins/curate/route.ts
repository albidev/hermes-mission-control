import { CurateRoute } from './CurateRoute';

/**
 * Curate plugin route wrapper.
 * For internal plugins, this is a thin re-export.
 * For external plugins (loaded at runtime), this would be a dynamic import.
 */
export const CuratePlugin = CurateRoute;

export const CurateRoutePath = '/curate';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type CurateRouteType = typeof CurateRoute;
