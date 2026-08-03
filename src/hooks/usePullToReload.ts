import { useCallback, useEffect, useRef, useState } from 'react';

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

  const handleStart = useCallback(
    (clientY: number) => {
      if (disabled || reloadingRef.current) return;
      if (!isAtTop(getTarget())) return;
      startYRef.current = clientY;
      currentYRef.current = clientY;
      pullStartedRef.current = false;
    },
    [disabled, getTarget, isAtTop],
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

    const onTouchStart = (event: TouchEvent) => {
      handleStart(event.touches[0].clientY);
    };
    const onTouchMove = (event: TouchEvent) => {
      if (pullStartedRef.current || startYRef.current !== null) {
        if (state.pulling) {
          event.preventDefault();
        }
        handleMove(event.touches[0].clientY);
      }
    };
    const onTouchEnd = () => handleEnd();

    const onMouseDown = (event: MouseEvent) => {
      handleStart(event.clientY);
    };
    const onMouseMove = (event: MouseEvent) => {
      if (pullStartedRef.current || startYRef.current !== null) {
        handleMove(event.clientY);
      }
    };
    const onMouseUp = () => handleEnd();

    target.addEventListener('touchstart', onTouchStart, { passive: true });
    target.addEventListener('touchmove', onTouchMove, { passive: false });
    target.addEventListener('touchend', onTouchEnd, { passive: true });

    target.addEventListener('mousedown', onMouseDown, { passive: true });
    target.addEventListener('mousemove', onMouseMove, { passive: true });
    target.addEventListener('mouseup', onMouseUp, { passive: true });

    return () => {
      target.removeEventListener('touchstart', onTouchStart);
      target.removeEventListener('touchmove', onTouchMove);
      target.removeEventListener('touchend', onTouchEnd);
      target.removeEventListener('mousedown', onMouseDown);
      target.removeEventListener('mousemove', onMouseMove);
      target.removeEventListener('mouseup', onMouseUp);
    };
  }, [getTarget, handleStart, handleMove, handleEnd, state.pulling]);

  return { state, reset };
}
