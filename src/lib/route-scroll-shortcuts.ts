const EDITABLE_OR_INDEPENDENT_SCROLL = 'input, textarea, select, [contenteditable], [role="textbox"], .cm-editor, .xterm, .side-menu, [role="dialog"]';

export function getRouteScroller(stage: HTMLElement | null): HTMLElement | undefined {
  const preferred = stage?.querySelector<HTMLElement>('.route-page-scroll');
  return preferred && preferred.scrollHeight > preferred.clientHeight ? preferred
    : Array.from(stage?.querySelectorAll<HTMLElement>('*') ?? []).find((element) =>
        element.scrollHeight > element.clientHeight && element.clientHeight > 0
        && ['auto', 'scroll'].includes(getComputedStyle(element).overflowY));
}

export function scrollRouteToTop(stage: HTMLElement | null): boolean {
  const scroller = getRouteScroller(stage);
  if (!scroller) return false;
  scroller.scrollTo({ top: 0, behavior: 'smooth' });
  return true;
}

/** Route content scrolls inside the fixed dashboard shell, not on window. */
export function handleRouteScrollShortcut(event: KeyboardEvent, stage: HTMLElement | null, blocked: boolean): boolean {
  if (blocked || event.defaultPrevented || !event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return false;
  if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return false;
  const target = event.target as Element | null;
  if (target?.closest?.(EDITABLE_OR_INDEPENDENT_SCROLL)) return false;

  const scroller = getRouteScroller(stage);
  if (!scroller) return false;
  event.preventDefault();
  scroller.scrollTo({ top: event.key === 'ArrowUp' ? 0 : scroller.scrollHeight, behavior: 'auto' });
  return true;
}
