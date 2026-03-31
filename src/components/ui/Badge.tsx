import { type HTMLAttributes, forwardRef } from 'react';

type BadgeVariant = 'default' | 'positive' | 'warning' | 'negative' | 'accent';

type BadgeProps = HTMLAttributes<HTMLSpanElement> & {
  variant?: BadgeVariant;
  dot?: boolean;
};

const variantClasses: Record<BadgeVariant, string> = {
  default: 'bg-surface border border-border text-text-muted',
  positive: 'bg-positive-subtle text-positive border border-positive/20',
  warning: 'bg-warning-subtle text-warning border border-warning/20',
  negative: 'bg-negative-subtle text-negative border border-negative/20',
  accent: 'bg-accent-subtle text-accent border border-accent/20',
};

export const Badge = forwardRef<HTMLSpanElement, BadgeProps>(
  ({ variant = 'default', dot = false, className = '', children, ...props }, ref) => {
    return (
      <span
        ref={ref}
        className={[
          'inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5',
          'text-xs font-medium',
          variantClasses[variant],
          className,
        ].join(' ')}
        {...props}
      >
        {dot && (
          <span
            className={[
              'h-1.5 w-1.5 rounded-full',
              variant === 'positive' ? 'bg-positive' :
              variant === 'warning' ? 'bg-warning' :
              variant === 'negative' ? 'bg-negative' :
              variant === 'accent' ? 'bg-accent' :
              'bg-current',
            ].join(' ')}
          />
        )}
        {children}
      </span>
    );
  },
);

Badge.displayName = 'Badge';
