import { useCallback, useState } from 'react';

const STORAGE_KEY = 'mission-control-chat-width';
const MIN_WIDTH = 360;
const MAX_WIDTH = 900;

function readStoredWidth(): number | null {
  if (typeof window === 'undefined') return null;
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (!stored) return null;
    const parsed = Number(stored);
    return Number.isFinite(parsed) && parsed >= MIN_WIDTH && parsed <= MAX_WIDTH ? parsed : null;
  } catch {
    return null;
  }
}

export function useChatDrawerResize(isExpanded: boolean) {
  const [drawerWidth, setDrawerWidth] = useState<number | null>(readStoredWidth);

  const startResize = useCallback((event: React.MouseEvent) => {
    if (isExpanded) return;
    event.preventDefault();
    let resizing = true;
    const onMove = (moveEvent: MouseEvent) => {
      if (!resizing) return;
      const width = Math.min(Math.max(window.innerWidth - moveEvent.clientX, MIN_WIDTH), Math.min(MAX_WIDTH, window.innerWidth - 16));
      setDrawerWidth(width);
    };
    const onUp = () => {
      resizing = false;
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      setDrawerWidth((current) => {
        if (current != null) {
          try { window.localStorage.setItem(STORAGE_KEY, String(current)); } catch { /* storage unavailable */ }
        }
        return current;
      });
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  }, [isExpanded]);

  return { drawerWidth, startResize };
}
