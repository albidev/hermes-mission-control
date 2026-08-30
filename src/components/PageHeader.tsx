import type { ReactNode } from 'react';

export function PageHeader({
  eyebrow,
  title,
  description,
  meta,
  actions,
}: {
  eyebrow: string;
  title: string;
  description?: string;
  meta?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="route-page-header w-full min-w-0 max-w-full px-2 pb-3 pt-3 sm:px-4 sm:pb-4 sm:pt-4">
      <div className="flex min-w-0 items-start justify-between gap-3 sm:gap-4">
        <div className="route-page-header-copy min-w-0 flex-1">
          <span className="eyebrow">{eyebrow}</span>
          <h2 className="mt-1 text-base font-semibold leading-tight text-text sm:truncate">{title}</h2>
          {description ? <p className="mt-1.5 max-w-2xl text-xs leading-relaxed text-text-muted">{description}</p> : null}
        </div>
        {(meta || actions) ? (
          <div className="route-page-header-actions flex max-w-[42%] min-w-0 shrink-0 flex-col items-end gap-1.5 text-[11px] text-text-subtle sm:max-w-none sm:flex-row sm:items-center sm:gap-3 sm:text-xs">
            {meta ? <span className="max-w-full truncate whitespace-nowrap">{meta}</span> : null}
            {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
