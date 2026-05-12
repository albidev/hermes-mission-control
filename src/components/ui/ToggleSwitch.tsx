import { type InputHTMLAttributes, forwardRef } from 'react';

type ToggleSwitchProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'className'> & {
  label?: string;
};

export const ToggleSwitch = forwardRef<HTMLInputElement, ToggleSwitchProps>(
  ({ label, id, ...props }, ref) => {
    return (
      <label
        htmlFor={id}
        className="relative inline-flex items-center gap-2 cursor-pointer select-none"
      >
        <input
          ref={ref}
          type="checkbox"
          id={id}
          className="peer sr-only"
          {...props}
        />
        <span className={[
          'relative block h-5 w-9 rounded-full transition-colors duration-200',
          'bg-border peer-checked:bg-accent',
          'after:absolute after:inset-y-0 after:my-auto after:h-3.5 after:w-3.5 after:rounded-full after:bg-white',
          'after:left-0.5 after:transition-transform after:duration-200',
          'peer-checked:after:translate-x-[calc(100%+2px)]',
          'peer-disabled:opacity-40 peer-disabled:cursor-not-allowed',
        ].join(' ')} />
        {label && (
          <span className="text-xs text-text-muted">{label}</span>
        )}
      </label>
    );
  },
);

ToggleSwitch.displayName = 'ToggleSwitch';
