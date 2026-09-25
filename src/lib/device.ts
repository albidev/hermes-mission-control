// Shared device/input heuristics. Kept tiny and dependency-free so it can be
// imported from any component without pulling in extra weight.

/**
 * True when the primary pointer is coarse (touch) rather than fine (mouse/trackpad).
 * Use this to gate behaviors that are convenient on desktop but actively harmful
 * on mobile — e.g. autofocusing a search input, which pops the on-screen keyboard
 * and eats screen real estate the moment a panel opens, before the user asked for it.
 *
 * Safe to call during render: matchMedia is synchronous and this has no listeners,
 * so it reflects the pointer type at initial mount. Pointer type essentially never
 * changes mid-session (no iPad-with-mouse hot-swap handling needed here).
 */
export function isCoarsePointer(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false;
  try {
    return window.matchMedia('(pointer: coarse), (max-width: 640px)').matches;
  } catch {
    return false;
  }
}
