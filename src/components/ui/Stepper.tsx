import { Check } from 'lucide-react';
import type { ReactNode } from 'react';
import { useI18n } from '../../lib/i18n';

export type StepperStep = { id: string; label: string; content?: ReactNode };
type StepperProps = {
  steps: StepperStep[];
  active: string;
  onSelect?: (id: string) => void;
};

export function Stepper({ steps, active, onSelect }: StepperProps) {
  const { t } = useI18n();
  const activeIndex = steps.findIndex((step) => step.id === active);
  return (
    <ol className="invite-stepper" aria-label={t('inviteUi.progress')}>
      {steps.map((step, index) => (
        <li key={step.id} className={index === activeIndex ? 'is-current' : index < activeIndex ? 'is-done' : ''}>
          <div className="invite-step-heading" aria-current={index === activeIndex ? 'step' : undefined}>
            <span className="invite-step-number" aria-hidden="true">
              {index < activeIndex ? <Check size={13} /> : index + 1}
            </span>
            {onSelect ? (
              <button type="button" className="min-h-11 text-left" onClick={() => onSelect(step.id)}>
                {step.label}
              </button>
            ) : <span>{step.label}</span>}
          </div>
          {index === activeIndex && step.content ? <div className="invite-step-content">{step.content}</div> : null}
        </li>
      ))}
    </ol>
  );
}
