import { useEffect, useId, useRef, useState } from 'react';
import { Copy } from 'lucide-react';
import { useI18n } from '../../lib/i18n';
import { Button } from '../ui/Button';

// Mounted only while the code sheet is open; key by code to isolate late results.
// Clipboard errors are local feedback, never pairing protocol/API failures.
export function PairingCode({ code }: { code: string }) {
  const { t } = useI18n();
  const hintId = useId();
  const field = useRef<HTMLTextAreaElement>(null);
  const mounted = useRef(false);
  const pending = useRef(false);
  const [status, setStatus] = useState<'idle' | 'copying' | 'copied' | 'manual'>('idle');
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const copy = async () => {
    if (pending.current) return;
    pending.current = true;
    setStatus('copying');
    let copied = false;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(code);
        copied = true;
      }
    } catch {
      // Permission refusal (or an insecure context) still permits manual selection.
    }
    // Never focus/select a disconnected field or update a replacement code sheet.
    if (!mounted.current) return;
    if (!copied) {
      const control = field.current;
      try {
        control?.focus({ preventScroll: true });
        control?.select();
        // Synchronous on the missing-API path, within the explicit click gesture.
        // A denial after await may lose activation: false/throw remains manual-copy.
        copied = !!control && document.execCommand?.('copy') === true;
      } catch {
        copied = false;
      }
    }
    pending.current = false;
    setStatus(copied ? 'copied' : 'manual');
  };

  return (
    <div className="grid gap-4">
      <textarea ref={field} className="invite-code min-h-28 w-full resize-y font-mono"
        aria-label={t('inviteUi.oneTimeCode')} aria-describedby={status === 'manual' ? hintId : undefined}
        value={code} readOnly spellCheck={false} autoComplete="off" />
      <Button variant="primary" className="w-full" icon={<Copy size={18} />}
        disabled={status === 'copying'} onClick={() => void copy()}>
        {t(status === 'copied' ? 'inviteUi.copied' : 'inviteUi.copy')}
      </Button>
      {status === 'manual' ? <p id={hintId} role="status" className="invite-notice">{t('inviteUi.copyManual')}</p> : null}
    </div>
  );
}
