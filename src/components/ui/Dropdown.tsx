import { ChevronDown } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

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
  const ref = useRef<HTMLDivElement>(null);
  const activeLabel = options.find((option) => option.value === value)?.label ?? placeholder;

  useEffect(() => {
    if (!open) return;
    const handler = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  return (
    <div className="relative" ref={ref}>
      <button
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
      {open ? (
        <ul className={`absolute z-30 min-w-[10rem] rounded-xl border border-border-subtle bg-surface p-1 shadow-xl ${dropUp ? 'bottom-full mb-1' : 'mt-1'}`} role="listbox" aria-label={ariaLabel}>
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
        </ul>
      ) : null}
    </div>
  );
}
