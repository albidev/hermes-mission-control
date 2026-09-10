import { ChevronDown } from 'lucide-react';
import { createPortal } from 'react-dom';
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';

export type DropdownOption = { value: string; label: string };

type DropdownProps = {
  value: string;
  options: DropdownOption[];
  onChange: (value: string) => void;
  placeholder?: string;
  ariaLabel: string;
  dropUp?: boolean;
  disabled?: boolean;
};

/** Shared select-style control used by Kanban, Bot configuration, and filters. */
export function Dropdown({
  value,
  options,
  onChange,
  placeholder = 'All',
  ariaLabel,
  dropUp = false,
  disabled = false,
}: DropdownProps) {
  const [open, setOpen] = useState(false);
  const [menuStyle, setMenuStyle] = useState<CSSProperties>({
    maxHeight: 'calc(100vh - 16px)',
    visibility: 'hidden',
  });
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLUListElement>(null);
  const activeLabel = options.find((option) => option.value === value)?.label ?? placeholder;

  useEffect(() => {
    if (!open) return;
    const handler = (event: PointerEvent) => {
      const target = event.target as Node;
      if (triggerRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener('pointerdown', handler);
    return () => document.removeEventListener('pointerdown', handler);
  }, [open]);

  useLayoutEffect(() => {
    if (!open) return;

    const updatePosition = () => {
      const trigger = triggerRef.current;
      const menu = menuRef.current;
      if (!trigger || !menu) return;

      const triggerRect = trigger.getBoundingClientRect();
      const menuHeight = menu.getBoundingClientRect().height;
      const viewportPadding = 8;
      const gap = 4;
      const spaceBelow = window.innerHeight - triggerRect.bottom - viewportPadding;
      const spaceAbove = triggerRect.top - viewportPadding;
      const opensUp = dropUp || (spaceBelow < menuHeight && spaceAbove > spaceBelow);
      const top = opensUp
        ? Math.max(viewportPadding, triggerRect.top - menuHeight - gap)
        : Math.min(
          triggerRect.bottom + gap,
          Math.max(viewportPadding, window.innerHeight - menuHeight - viewportPadding),
        );
      const width = Math.max(triggerRect.width, 160);
      const left = Math.min(
        Math.max(viewportPadding, triggerRect.left),
        Math.max(viewportPadding, window.innerWidth - width - viewportPadding),
      );

      setMenuStyle({
        position: 'fixed',
        top,
        left,
        minWidth: width,
        maxHeight: Math.max(80, window.innerHeight - viewportPadding * 2),
        visibility: 'visible',
      });
    };

    const frame = window.requestAnimationFrame(updatePosition);
    window.addEventListener('resize', updatePosition);
    window.addEventListener('scroll', updatePosition, true);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener('resize', updatePosition);
      window.removeEventListener('scroll', updatePosition, true);
    };
  }, [dropUp, open, options.length]);

  return (
    <div className="relative">
      <button
        ref={triggerRef}
        type="button"
        className={`flex w-full items-center justify-between gap-1.5 rounded-lg border px-2.5 py-2 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${value ? 'border-border bg-surface-sunken text-text' : 'border-border-subtle bg-surface text-text-muted hover:border-border hover:text-text'}`}
        onClick={() => setOpen((current) => !current)}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={ariaLabel}
        disabled={disabled}
      >
        <span className="truncate">{activeLabel}</span>
        <ChevronDown size={13} className={`shrink-0 text-text-subtle transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open ? createPortal(
        <ul
          ref={menuRef}
          className="fixed z-[200] overflow-y-auto rounded-xl border border-border-subtle bg-surface p-1 shadow-xl"
          style={menuStyle}
          role="listbox"
          aria-label={ariaLabel}
        >
          {options.map((option) => {
            const selected = option.value === value;
            return (
              <li key={option.value || '__empty__'} role="option" aria-selected={selected}>
                <button
                  type="button"
                  className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition-colors ${selected ? 'bg-sky-400/10 font-medium text-text' : 'text-text-muted hover:bg-surface-sunken hover:text-text'}`}
                  onClick={() => { onChange(option.value); setOpen(false); }}
                >
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${selected ? 'bg-sky-400' : 'bg-transparent'}`} />
                  <span className="flex-1 truncate">{option.label}</span>
                </button>
              </li>
            );
          })}
        </ul>,
        document.body,
      ) : null}
    </div>
  );
}
