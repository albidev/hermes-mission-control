import { useI18n } from '../lib/i18n';
import { useEffect, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

type ModalProps = {
  open: boolean;
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
};

export function Modal({ open, title, subtitle, onClose, children, footer }: ModalProps) {
  const { t } = useI18n();
  useEffect(() => {
    if (!open || typeof window === 'undefined') {
      return;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose, open]);

  useEffect(() => {
    if (!open || typeof document === 'undefined') {
      return;
    }

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [open]);

  if (!open || typeof document === 'undefined') {
    return null;
  }

  return createPortal(
    <div
      className="fixed inset-0 z-[120] bg-black/60 backdrop-blur-[1px] flex items-end sm:items-center sm:justify-center"
      role="presentation"
      onMouseDown={onClose}
    >
      <section
        className={[
          'w-full h-[92vh] sm:h-auto sm:max-h-[88vh] sm:w-[min(920px,92vw)]',
          'mobile-modal',
          'rounded-t-2xl sm:rounded-2xl border border-border bg-surface shadow-2xl',
          'flex flex-col overflow-hidden',
        ].join(' ')}
        role="dialog"
        aria-modal="true"
        aria-labelledby="modal-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="px-4 py-3 border-b border-border-subtle flex items-start justify-between gap-3 sticky top-0 bg-surface z-10">
          <div className="min-w-0">
            <p className="eyebrow">{t('modal.detailView')}</p>
            <h3 id="modal-title" className="text-sm font-semibold text-text break-words">{title}</h3>
            {subtitle ? <p className="text-xs text-text-subtle break-words mt-0.5">{subtitle}</p> : null}
          </div>
          <button
            className="inline-flex items-center justify-center rounded-lg h-8 w-8 border border-border-subtle bg-surface-raised text-text-muted hover:text-text hover:bg-surface"
            type="button"
            onClick={onClose}
            aria-label={t('modal.closeDialog')}
          >
            ×
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4">{children}</div>

        {footer ? <div className="px-4 py-3 border-t border-border-subtle bg-surface-raised/40">{footer}</div> : null}
      </section>
    </div>,
    document.body,
  );
}
