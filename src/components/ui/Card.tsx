import { type HTMLAttributes, forwardRef } from 'react';

type CardProps = HTMLAttributes<HTMLDivElement> & {
  variant?: 'default' | 'raised' | 'sunken';
  padding?: 'none' | 'sm' | 'md' | 'lg';
};

const variantClasses: Record<NonNullable<CardProps['variant']>, string> = {
  default: 'bg-surface-raised border border-border',
  raised: 'bg-surface border border-border shadow-sm',
  sunken: 'bg-surface-sunken border border-border-subtle',
};

const paddingClasses: Record<NonNullable<CardProps['padding']>, string> = {
  none: '',
  sm: 'p-3',
  md: 'p-4',
  lg: 'p-6',
};

export const Card = forwardRef<HTMLDivElement, CardProps>(
  ({ variant = 'default', padding = 'md', className = '', children, ...props }, ref) => {
    return (
      <div
        ref={ref}
        className={[
          'rounded-lg',
          variantClasses[variant],
          paddingClasses[padding],
          className,
        ].join(' ')}
        {...props}
      >
        {children}
      </div>
    );
  },
);

Card.displayName = 'Card';

export const CardHeader = forwardRef<HTMLDivElement, HTMLAttributes<HTMLDivElement>>(
  ({ className = '', children, ...props }, ref) => (
    <div ref={ref} className={`flex flex-col gap-1 ${className}`} {...props}>
      {children}
    </div>
  ),
);

CardHeader.displayName = 'CardHeader';

export const CardTitle = forwardRef<HTMLHeadingElement, HTMLAttributes<HTMLHeadingElement>>(
  ({ className = '', children, ...props }, ref) => (
    <h3 ref={ref} className={`text-sm font-semibold text-text ${className}`} {...props}>
      {children}
    </h3>
  ),
);

CardTitle.displayName = 'CardTitle';
