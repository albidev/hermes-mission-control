import { useLayoutEffect, useRef, type ReactNode } from 'react';

/** A drawer-local blocking layer. The rest of the app stays interactive. */
export function RoomCreationLayer({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const layer = ref.current;
    const drawer = layer?.parentElement;
    if (!layer || !drawer) return;
    // Direct drawer siblings only: never inert the app, body, or resize handle.
    // The mode rail owns its blocked prop in React, including auto-hide rerenders
    // and cleanup; do not snapshot or restore its independently changing state.
    const previous = new Map<HTMLElement, { inert: boolean; hidden: string | null }>();
    const protect = () => {
      for (const node of drawer.children) {
        if (!(node instanceof HTMLElement) || node === layer || node.classList.contains('chat-drawer-resize-handle') || node.classList.contains('chat-mode-tabs-shell') || previous.has(node)) continue;
        previous.set(node, { inert: node.inert, hidden: node.getAttribute('aria-hidden') });
        node.inert = true; node.setAttribute('aria-hidden', 'true');
      }
    };
    protect();
    // Capability probes can insert a toolbar while the task is already open.
    const observer = new window.MutationObserver(protect);
    observer.observe(drawer, { childList: true });
    const viewport = window.visualViewport;
    let revealFrame = 0;
    const revealFocusedField = () => {
      window.cancelAnimationFrame(revealFrame);
      revealFrame = window.requestAnimationFrame(() => {
        const field = document.activeElement;
        if (!(field instanceof HTMLElement) || !layer.contains(field)
            || !field.matches('input, textarea, select, [contenteditable="true"]')) return;
        const scroller = field.closest<HTMLElement>('.room-create-scroll');
        if (!scroller) return;
        const visible = scroller.getBoundingClientRect();
        const bounds = field.getBoundingClientRect();
        // Scroll only the task body, never the transcript or the document.
        // Wait for the new viewport/container-query layout before measuring.
        if (bounds.bottom > visible.bottom - 8) scroller.scrollTop += bounds.bottom - visible.bottom + 8;
        else if (bounds.top < visible.top + 8) scroller.scrollTop += bounds.top - visible.top - 8;
      });
    };
    const fit = () => {
      const bounds = drawer.getBoundingClientRect();
      // iOS can pan the document when focusing a nested field: the drawer's
      // negative rect.top already reflects that pan. Do not apply it twice
      // through visualViewport.offsetTop when fitting this local overlay.
      const visibleTop = Math.max(0, (viewport?.offsetTop ?? 0) + Math.min(0, bounds.top));
      layer.style.top = `${Math.max(0, visibleTop - bounds.top)}px`;
      layer.style.bottom = `${Math.max(0, bounds.bottom - (visibleTop + (viewport?.height ?? window.innerHeight)))}px`;
      revealFocusedField();
    };
    fit();
    layer.addEventListener('focusin', revealFocusedField);
    viewport?.addEventListener('resize', fit);
    viewport?.addEventListener('scroll', fit);
    const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(fit);
    resize?.observe(drawer);
    return () => {
      observer.disconnect(); resize?.disconnect();
      window.cancelAnimationFrame(revealFrame);
      layer.removeEventListener('focusin', revealFocusedField);
      viewport?.removeEventListener('resize', fit);
      viewport?.removeEventListener('scroll', fit);
      for (const [node, { inert, hidden }] of previous) {
        node.inert = inert;
        if (hidden === null) node.removeAttribute('aria-hidden'); else node.setAttribute('aria-hidden', hidden);
      }
    };
  }, []);
  return <div ref={ref} className="room-create-layer" onPointerDown={(event) => {
    // Scrim clicks neither dismiss a draft nor blur its focused control.
    if (event.target === event.currentTarget) event.preventDefault();
  }}>{children}</div>;
}
