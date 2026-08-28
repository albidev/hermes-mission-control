import { useCallback, useEffect, useRef, useState } from 'react';
import { recordReloadDiagnostic } from '../lib/reload-diagnostics';

const PULL_THRESHOLD = 72;
const RESISTANCE = 0.55;
const MAX_PULL = 140;
const START_BUFFER = 8;

export type PullToReloadState = {
  pulling: boolean;
  progress: number;
  reloading: boolean;
};

export type UsePullToReloadOptions = {
  containerRef?: React.RefObject<HTMLElement | null>;
  onReload: () => void | Promise<void>;
  disabled?: boolean;
};

export function usePullToReload({ containerRef, onReload, disabled }: UsePullToReloadOptions) {
  const [state, setState] = useState<PullToReloadState>({ pulling: false, progress: 0, reloading: false });
  const startYRef = useRef<number | null>(null);
  const currentYRef = useRef<number | null>(null);
  const pullStartedRef = useRef(false);
  const reloadingRef = useRef(false);
  const reloadRef = useRef(onReload);
  reloadRef.current = onReload;

  const reset = useCallback(() => {
    startYRef.current = null;
    currentYRef.current = null;
    pullStartedRef.current = false;
    reloadingRef.current = false;
    setState({ pulling: false, progress: 0, reloading: false });
  }, []);

  const isAtTop = useCallback((element: HTMLElement | Window) => {
    if (element === window || ('scrollY' in element && element === window)) {
      return window.scrollY <= 0;
    }
    const el = element as HTMLElement;
    return el.scrollTop <= 0;
  }, []);

  const getTarget = useCallback((): HTMLElement | Window => {
    if (containerRef?.current) return containerRef.current;
    return window;
  }, [containerRef]);

  const hasScrollableAncestor = useCallback((target: EventTarget | null): boolean => {
    const root = containerRef?.current;
    if (!root || !(target instanceof HTMLElement)) return false;

    let element: HTMLElement | null = target;
    while (element && element !== root) {
      const style = window.getComputedStyle(element);
      const scrollable = /(auto|scroll|overlay)/.test(style.overflowY) && element.scrollHeight > element.clientHeight;
      if (scrollable) return true;
      element = element.parentElement;
    }
    return false;
  }, [containerRef]);

  const handleStart = useCallback(
    (clientY: number, target?: EventTarget | null) => {
      if (disabled || reloadingRef.current) return;
      if (hasScrollableAncestor(target ?? null)) return;
      if (!isAtTop(getTarget())) return;
      startYRef.current = clientY;
      currentYRef.current = clientY;
      pullStartedRef.current = false;
      recordReloadDiagnostic('touchstart-at-top', { clientY });
    },
    [disabled, getTarget, hasScrollableAncestor, isAtTop],
  );

  const handleMove = useCallback(
    (clientY: number) => {
      if (disabled || reloadingRef.current || startYRef.current === null) return;

      const delta = clientY - startYRef.current;
      if (delta <= 0) {
        if (pullStartedRef.current) {
          reset();
        }
        return;
      }

      if (!pullStartedRef.current && delta > START_BUFFER) {
        if (!isAtTop(getTarget())) {
          startYRef.current = null;
          return;
        }
        pullStartedRef.current = true;
        recordReloadDiagnostic('touch-pull-start', {
          delta,
          stack: new Error('Mission Control pull gesture').stack,
        });
      }

      if (!pullStartedRef.current) return;

      currentYRef.current = clientY;
      const damped = Math.min(delta * RESISTANCE, MAX_PULL);
      const progress = Math.min(damped / PULL_THRESHOLD, 1);
      setState({ pulling: true, progress, reloading: false });
    },
    [disabled, getTarget, isAtTop, reset],
  );

  const handleEnd = useCallback(async () => {
    if (disabled || reloadingRef.current || startYRef.current === null || currentYRef.current === null) return;

    const delta = currentYRef.current - startYRef.current;
    const damped = Math.min(delta * RESISTANCE, MAX_PULL);
    const thresholdReached = damped >= PULL_THRESHOLD;

    if (!thresholdReached) {
      reset();
      return;
    }

    reloadingRef.current = true;
    setState({ pulling: false, progress: 1, reloading: true });
    startYRef.current = null;
    currentYRef.current = null;
    pullStartedRef.current = false;

    try {
      await reloadRef.current();
    } finally {
      reset();
    }
  }, [disabled, reset]);

  useEffect(() => {
    const target = getTarget();

    const onTouchStart = (event: Event) => {
      const touch = (event as TouchEvent).touches[0];
      if (touch) handleStart(touch.clientY, event.target);
    };
    const onTouchMove = (event: Event) => {
      if (pullStartedRef.current || startYRef.current !== null) {
        const touch = (event as TouchEvent).touches[0];
        if (!touch) return;
        const clientY = touch.clientY;
        // Do not wait for React to re-render `state.pulling`: the first move
        // after the threshold can otherwise reach iOS' native pull-to-refresh
        // before the stateful listener sees `pulling === true`.
        handleMove(clientY);
        // Preserve native scrolling until the gesture is intentional. Calling
        // preventDefault() while the gesture is merely armed makes a small
        // downward jitter at the top cancel the user's subsequent scroll.
        if (
          pullStartedRef.current &&
          startYRef.current !== null &&
          clientY > startYRef.current &&
          isAtTop(getTarget())
        ) {
          event.preventDefault();
          recordReloadDiagnostic('touch-prevent-default', { delta: clientY - startYRef.current });
        }
      }
    };
    const onTouchEnd = () => {
      if (startYRef.current !== null || pullStartedRef.current) {
        recordReloadDiagnostic('touchend', { pullStarted: pullStartedRef.current });
      }
      void handleEnd();
    };
    const onTouchCancel = () => reset();

    const onMouseDown = (event: Event) => {
      const mouse = event as MouseEvent;
      handleStart(mouse.clientY, event.target);
    };
    const onMouseMove = (event: Event) => {
      if (pullStartedRef.current || startYRef.current !== null) {
        handleMove((event as MouseEvent).clientY);
      }
    };
    const onMouseUp = () => handleEnd();

    target.addEventListener('touchstart', onTouchStart, { passive: true });
    target.addEventListener('touchmove', onTouchMove, { passive: false });
    target.addEventListener('touchend', onTouchEnd, { passive: true });
    target.addEventListener('touchcancel', onTouchCancel, { passive: true });

    target.addEventListener('mousedown', onMouseDown, { passive: true });
    target.addEventListener('mousemove', onMouseMove, { passive: true });
    target.addEventListener('mouseup', onMouseUp, { passive: true });

    return () => {
      target.removeEventListener('touchstart', onTouchStart);
      target.removeEventListener('touchmove', onTouchMove);
      target.removeEventListener('touchend', onTouchEnd);
      target.removeEventListener('touchcancel', onTouchCancel);
      target.removeEventListener('mousedown', onMouseDown);
      target.removeEventListener('mousemove', onMouseMove);
      target.removeEventListener('mouseup', onMouseUp);
    };
  }, [getTarget, handleStart, handleMove, handleEnd]);

  return { state, reset };
}
