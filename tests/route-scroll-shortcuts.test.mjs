import { strict as assert } from 'node:assert';
import { handleRouteScrollShortcut, getRouteScroller, scrollRouteToTop } from '../src/lib/route-scroll-shortcuts.ts';

function fixture({ key = 'ArrowUp', metaKey = true, closest = () => null, blocked = false, hasScroller = true } = {}) {
  const calls = [];
  const scroller = {
    scrollHeight: 1200,
    clientHeight: 300,
    scrollTo: (options) => calls.push(options),
  };
  const stage = { querySelector: () => hasScroller ? scroller : null, querySelectorAll: () => [] };
  const event = {
    key, metaKey, ctrlKey: false, altKey: false, shiftKey: false,
    defaultPrevented: false, target: { closest },
    preventDefault() { this.defaultPrevented = true; },
  };
  return { event, stage, calls, scroller, blocked };
}

for (const [key, expectedTop] of [['ArrowUp', 0], ['ArrowDown', 1200]]) {
  const { event, stage, calls } = fixture({ key });
  assert.equal(handleRouteScrollShortcut(event, stage, false), true, `Cmd+${key} should navigate the route`);
  assert.equal(event.defaultPrevented, true);
  assert.deepEqual(calls, [{ top: expectedTop, behavior: 'auto' }]);
}

for (const options of [
  { metaKey: false },
  { key: 'ArrowLeft' },
  { closest: () => ({ tagName: 'TEXTAREA' }) },
  { closest: () => ({ className: 'cm-editor' }) },
  { blocked: true },
  { hasScroller: false },
]) {
  const { event, stage, calls, blocked } = fixture(options);
  assert.equal(handleRouteScrollShortcut(event, stage, blocked), false);
  assert.equal(event.defaultPrevented, false);
  assert.deepEqual(calls, []);
}

const short = fixture();
short.scroller.scrollHeight = short.scroller.clientHeight;
assert.equal(handleRouteScrollShortcut(short.event, short.stage, false), false);

// Plugins can supply their own scroll container instead of .route-page-scroll.
const plugin = fixture({ hasScroller: false });
plugin.stage.querySelectorAll = () => [plugin.scroller];
const previousStyle = globalThis.getComputedStyle;
globalThis.getComputedStyle = () => ({ overflowY: 'auto' });
try {
  assert.equal(handleRouteScrollShortcut(plugin.event, plugin.stage, false), true);
  assert.deepEqual(plugin.calls, [{ top: 0, behavior: 'auto' }]);
} finally {
  globalThis.getComputedStyle = previousStyle;
}
const mobile = fixture();
assert.equal(getRouteScroller(mobile.stage), mobile.scroller);
assert.equal(scrollRouteToTop(mobile.stage), true);
assert.deepEqual(mobile.calls, [{ top: 0, behavior: 'smooth' }]);
assert.equal(scrollRouteToTop(null), false);

console.log('route scroll shortcut tests passed');
