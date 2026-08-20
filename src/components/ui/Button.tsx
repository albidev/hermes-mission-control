import { type ButtonHTMLAttributes, forwardRef } from 'react';
import { Loader2 } from 'lucide-react';

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
type ButtonSize = 'sm' | 'md' | 'lg';

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  icon?: React.ReactNode;
  iconPosition?: 'left' | 'right';
  iconOnly?: boolean;
};

const variantClasses: Record<ButtonVariant, string> = {
  primary:
    'bg-accent text-white hover:bg-accent-hover focus-visible:ring-accent/50',
  secondary:
    'bg-surface border border-border text-text hover:bg-surface-raised hover:border-border-subtle focus-visible:ring-border-subtle',
  ghost:
    'bg-surface border border-border text-text-muted hover:bg-surface-raised hover:text-text hover:border-border-subtle focus-visible:ring-border',
  danger:
    'bg-negative-subtle text-negative border border-negative/20 hover:bg-negative/10 focus-visible:ring-negative/50',
};

const sizeClasses: Record<ButtonSize, string> = {
  sm: 'min-h-9 px-2.5 text-xs gap-1.5',
  md: 'min-h-9 px-3.5 text-sm gap-2',
  lg: 'min-h-11 px-5 text-base gap-2.5',
};

const iconOnlyClasses: Record<ButtonSize, string> = {
  sm: 'w-9 min-w-9 px-0',
  md: 'w-9 min-w-9 px-0',
  lg: 'w-11 min-w-11 px-0',
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  (
    {
      variant = 'secondary',
      size = 'md',
      loading = false,
      icon,
      iconPosition = 'left',
      iconOnly = false,
      className = '',
      disabled,
      children,
      ...props
    },
    ref,
  ) => {
    const isDisabled = disabled || loading;

    return (
      <button
        ref={ref}
        disabled={isDisabled}
        className={[
          'inline-flex items-center justify-center rounded-[var(--control-radius)] font-medium',
          'transition-colors duration-150',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2',
          'disabled:pointer-events-none disabled:opacity-50',
          variantClasses[variant],
          sizeClasses[size],
          iconOnly ? 'mc-icon-only' : '',
          iconOnly ? iconOnlyClasses[size] : '',
          className,
        ].join(' ')}
        {...props}
      >
        {loading ? (
          <Loader2 className="h-4 w-4 animate-spin" />
        ) : icon && iconPosition === 'left' ? (
          <span className="flex-shrink-0">{icon}</span>
        ) : null}
        {children}
        {!loading && icon && iconPosition === 'right' ? (
          <span className="flex-shrink-0">{icon}</span>
        ) : null}
      </button>
    );
  },
);

Button.displayName = 'Button';
