import { useEffect, useMemo, useRef, useState, type ReactNode, type PointerEvent as ReactPointerEvent } from 'react';
import { Check, GripVertical, LayoutDashboard, RotateCcw, X } from 'lucide-react';
import { Button } from '../ui/Button';
import { useI18n } from '../../lib/i18n';

const STORAGE_KEY = 'mission-control-dashboard-layout:v1';

export type DashboardWidget = {
  id: string;
  label: string;
  content: ReactNode;
  className?: string;
  locked?: boolean;
};

function loadOrder(ids: string[], fallback: string[]): string[] {
  if (typeof window === 'undefined') return fallback;
  try {
    const saved = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || 'null');
    if (!Array.isArray(saved)) return fallback;
    const known = new Set(ids);
    const valid = saved.filter((id): id is string => typeof id === 'string' && known.has(id));
    return [...valid, ...fallback.filter((id) => !valid.includes(id))];
  } catch {
    return fallback;
  }
}

export function DashboardGrid({ widgets }: { widgets: DashboardWidget[] }) {
  const { t } = useI18n();
  const widgetIds = widgets.map((widget) => widget.id);
  const widgetIdsSignature = widgetIds.join('\u0000');
  const ids = useMemo(() => widgetIds, [widgetIdsSignature]);
  const defaultOrder = ids;
  const [order, setOrder] = useState(() => loadOrder(ids, defaultOrder));
  const [arranging, setArranging] = useState(false);
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const pointerId = useRef<number | null>(null);

  useEffect(() => {
    setOrder((current) => {
      const currentKnown = current.filter((id) => ids.includes(id));
      const newlyAvailable = ids.filter((id) => !currentKnown.includes(id));
      return loadOrder(ids, [...currentKnown, ...newlyAvailable]);
    });
  }, [ids]);

  useEffect(() => {
    if (typeof window !== 'undefined') window.localStorage.setItem(STORAGE_KEY, JSON.stringify(order));
  }, [order]);

  const orderedWidgets = order
    .map((id) => widgets.find((widget) => widget.id === id))
    .filter((widget): widget is DashboardWidget => Boolean(widget));

  const move = (id: string, direction: -1 | 1) => {
    setOrder((current) => {
      const index = current.indexOf(id);
      const nextIndex = index + direction;
      if (index < 0 || nextIndex < 0 || nextIndex >= current.length) return current;
      const next = [...current];
      [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
      return next;
    });
  };

  const reorderFromPoint = (clientX: number, clientY: number, sourceId: string) => {
    const target = document.elementFromPoint(clientX, clientY)?.closest<HTMLElement>('[data-dashboard-widget]');
    const targetId = target?.dataset.dashboardWidget;
    if (!targetId || targetId === sourceId) return;
    setOrder((current) => {
      const from = current.indexOf(sourceId);
      const to = current.indexOf(targetId);
      if (from < 0 || to < 0 || from === to) return current;
      const next = [...current];
      next.splice(from, 1);
      next.splice(to, 0, sourceId);
      return next;
    });
  };

  const endDrag = (event?: ReactPointerEvent<HTMLButtonElement>) => {
    if (event && pointerId.current === event.pointerId && event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    pointerId.current = null;
    setDraggingId(null);
  };

  const handlePointerDown = (event: ReactPointerEvent<HTMLButtonElement>, id: string) => {
    if (!arranging) return;
    event.preventDefault();
    pointerId.current = event.pointerId;
    event.currentTarget.setPointerCapture(event.pointerId);
    setDraggingId(id);
  };

  const handlePointerMove = (event: ReactPointerEvent<HTMLButtonElement>, id: string) => {
    if (pointerId.current !== event.pointerId || draggingId !== id) return;
    reorderFromPoint(event.clientX, event.clientY, id);
  };

  const reset = () => {
    setOrder(defaultOrder);
    if (typeof window !== 'undefined') window.localStorage.removeItem(STORAGE_KEY);
  };

  return (
    <section className="dashboard-workspace" aria-label={t('ui.dashboardWorkspace')}>
      <div className="dashboard-toolbar">
        <div className="flex min-w-0 items-center gap-2">
          <LayoutDashboard className="h-4 w-4 shrink-0 text-accent" />
          <div className="min-w-0">
            <p className="eyebrow">{t('ui.operatorDashboard')}</p>
            <p className="truncate text-sm font-semibold text-text">
              {arranging ? t('ui.arrangeCockpit') : t('ui.yourCockpit')}
            </p>
          </div>
          {arranging ? <span className="dashboard-saved-badge"><Check className="h-3 w-3" /> {t('ui.autoSaved')}</span> : null}
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {arranging ? (
            <Button variant="ghost" size="sm" icon={<RotateCcw className="h-3.5 w-3.5" />} onClick={reset}>
              Reset
            </Button>
          ) : null}
          <Button
            variant={arranging ? 'primary' : 'ghost'}
            size="sm"
            icon={arranging ? <X className="h-3.5 w-3.5" /> : <GripVertical className="h-3.5 w-3.5" />}
            onClick={() => { setArranging((current) => !current); setDraggingId(null); }}
          >
            {arranging ? 'Done' : 'Arrange'}
          </Button>
        </div>
      </div>

      {arranging ? (
        <p className="dashboard-arrange-hint">
          Trascina il grip oppure usa le frecce. Il layout viene salvato automaticamente su questo dispositivo.
        </p>
      ) : null}

      <div className={`dashboard-grid ${arranging ? 'is-arranging' : ''}`}>
        {orderedWidgets.map((widget) => (
          <article
            key={widget.id}
            data-dashboard-widget={widget.id}
            className={`dashboard-widget ${widget.className || ''} ${draggingId === widget.id ? 'is-dragging' : ''}`}
          >
            {arranging ? (
              <div className="dashboard-widget-tools">
                <button
                  type="button"
                  className="dashboard-drag-handle"
                  aria-label={`Drag ${widget.label}`}
                  title={`Drag ${widget.label}`}
                  onPointerDown={(event) => handlePointerDown(event, widget.id)}
                  onPointerMove={(event) => handlePointerMove(event, widget.id)}
                  onPointerUp={endDrag}
                  onPointerCancel={endDrag}
                >
                  <GripVertical className="h-4 w-4" />
                  <span>{widget.label}</span>
                </button>
                <div className="dashboard-move-buttons">
                  <button type="button" aria-label={`Move ${widget.label} up`} onClick={() => move(widget.id, -1)}>↑</button>
                  <button type="button" aria-label={`Move ${widget.label} down`} onClick={() => move(widget.id, 1)}>↓</button>
                </div>
              </div>
            ) : null}
            <div className="dashboard-widget-content">{widget.content}</div>
          </article>
        ))}
      </div>
    </section>
  );
}
