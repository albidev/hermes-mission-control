import { Check, ChevronDown, ChevronUp, Circle, CircleDot, ListTodo, X } from 'lucide-react';
import { useEffect, useId, useState } from 'react';
import { useI18n } from '../../lib/i18n';
import type { TodoPlan, TodoPlanItem, TodoStatus } from '../../lib/todo-plan';

type ChatTodoPlanProps = {
  plan: TodoPlan | null;
  waitingForInput?: boolean;
};

function itemDepth(item: TodoPlanItem, items: TodoPlanItem[]): number {
  const parents = new Map(items.map((candidate) => [candidate.id, candidate.parent]));
  let depth = 0;
  let parent = item.parent;
  const seen = new Set<string>();
  while (parent && !seen.has(parent) && depth < 4) {
    seen.add(parent);
    depth += 1;
    parent = parents.get(parent);
  }
  return depth;
}

function statusIcon(status: TodoStatus) {
  if (status === 'completed') return <Check size={13} aria-hidden />;
  if (status === 'cancelled') return <X size={13} aria-hidden />;
  if (status === 'in_progress') return <CircleDot size={13} aria-hidden />;
  return <Circle size={13} aria-hidden />;
}

export function ChatTodoPlan({ plan, waitingForInput = false }: ChatTodoPlanProps) {
  const { t } = useI18n();
  const [expanded, setExpanded] = useState(false);
  const panelId = useId();

  useEffect(() => {
    if (!plan || plan.items.length === 0) setExpanded(false);
  }, [plan]);

  if (!plan || plan.items.length === 0) return null;

  const visibleStatus = waitingForInput ? 'waiting' : plan.status;
  const statusLabel = visibleStatus === 'waiting'
    ? t('chatPlan.status.waiting')
    : visibleStatus === 'running'
      ? t('chatPlan.status.running')
      : visibleStatus === 'planning'
        ? t('chatPlan.status.planning')
        : t('chatPlan.status.complete');
  const summaryItem = plan.current ?? plan.next;
  const progressLabel = t('chatPlan.progress', { completed: plan.completed, total: plan.total });
  const capsuleLabel = `${statusLabel} · ${t('chatPlan.expand')}: ${progressLabel}${summaryItem ? ` · ${summaryItem.content}` : ''}`;

  return (
    <section className={`chat-plan ${expanded ? 'is-expanded' : ''} chat-plan-${visibleStatus}`} aria-label={t('chatPlan.ariaLabel')}>
      <button
        type="button"
        className="chat-plan-capsule"
        aria-expanded={expanded}
        aria-controls={panelId}
        aria-label={expanded ? t('chatPlan.collapse') : capsuleLabel}
        onClick={() => setExpanded((current) => !current)}
      >
        <span className="chat-plan-mark" aria-hidden><ListTodo size={15} /></span>
        <span className="chat-plan-copy">
          <span className="chat-plan-title">{statusLabel}</span>
          {summaryItem ? <span className="chat-plan-current">{summaryItem.content}</span> : null}
        </span>
        <span className="chat-plan-count" title={progressLabel}>{plan.completed}/{plan.total}</span>
        <span className="chat-plan-chevron" aria-hidden>{expanded ? <ChevronDown size={15} /> : <ChevronUp size={15} />}</span>
      </button>

      {expanded ? (
        <div id={panelId} className="chat-plan-expanded">
          <div className="chat-plan-heading">
            <div>
              <strong>{t('chatPlan.title')}</strong>
              <span>{statusLabel}</span>
            </div>
            <span className="chat-plan-progress-label">{progressLabel}</span>
          </div>
          <div className="chat-plan-progress" role="progressbar" aria-label={t('chatPlan.progressLabel')} aria-valuemin={0} aria-valuemax={plan.total} aria-valuenow={plan.completed}>
            <span style={{ width: `${Math.min(100, (plan.completed / plan.total) * 100)}%` }} />
          </div>
          <ul className="chat-plan-list">
            {plan.items.map((item) => {
              const itemStatusLabel = item.status === 'completed'
                ? t('chatPlan.item.completed')
                : item.status === 'cancelled'
                  ? t('chatPlan.item.cancelled')
                  : item.status === 'in_progress'
                    ? t('chatPlan.item.inProgress')
                    : t('chatPlan.item.pending');
              return (
                <li key={item.id} className={`chat-plan-item chat-plan-item-${item.status}`} style={{ paddingLeft: `${itemDepth(item, plan.items) * 0.9}rem` }}>
                  <span className="chat-plan-item-icon" title={itemStatusLabel} aria-label={itemStatusLabel}>{statusIcon(item.status)}</span>
                  <span className="chat-plan-item-content">{item.content}</span>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
