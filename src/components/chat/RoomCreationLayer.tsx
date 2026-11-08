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
    const fit = () => {
      const bounds = drawer.getBoundingClientRect();
      layer.style.top = `${Math.max(0, (viewport?.offsetTop ?? 0) - bounds.top)}px`;
      layer.style.bottom = `${Math.max(0, bounds.bottom - ((viewport?.offsetTop ?? 0) + (viewport?.height ?? window.innerHeight)))}px`;
    };
    fit();
    viewport?.addEventListener('resize', fit);
    viewport?.addEventListener('scroll', fit);
    const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(fit);
    resize?.observe(drawer);
    return () => {
      observer.disconnect(); resize?.disconnect();
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
