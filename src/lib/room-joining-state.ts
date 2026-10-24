export type PairingWizardStep = 'start' | 'offer' | 'accept' | 'reveal' | 'sas' | 'complete' | 'cancelled' | 'expired';
export type PairingWizardState = {
  step: PairingWizardStep;
  id: string | null;
  expiresAt: number | null;
  sas: string | null;
  error: string | null;
};
export const initialPairingWizardState: PairingWizardState = {
  step: 'start', id: null, expiresAt: null, sas: null, error: null,
};
export type PairingWizardAction =
  | { type: 'start_offer'; id: string; expiresAt: number }
  | { type: 'import_offer'; id: string; expiresAt: number }
  | { type: 'accept'; sas: string }
  | { type: 'reveal'; sas: string }
  | { type: 'confirm' }
  | { type: 'cancel' }
  | { type: 'expire' }
  | { type: 'error'; code: string }
  | { type: 'reset' };

export function canConfirmSas(value: string): boolean {
  return /^\d{6}$/.test(value);
}

export function pairingWizardReducer(state: PairingWizardState, action: PairingWizardAction): PairingWizardState {
  if (action.type === 'reset') return initialPairingWizardState;
  if (['complete', 'cancelled', 'expired'].includes(state.step)) return state;
  if (action.type === 'cancel' || action.type === 'expire') {
    return { ...state, step: action.type === 'cancel' ? 'cancelled' : 'expired', sas: null, error: null };
  }
  if (action.type === 'error') return { ...state, error: action.code };
  if (state.step === 'start' && (action.type === 'start_offer' || action.type === 'import_offer')) {
    return {
      step: action.type === 'start_offer' ? 'offer' : 'accept',
      id: action.id, expiresAt: action.expiresAt, sas: null, error: null,
    };
  }
  if (action.type === 'accept' && state.step === 'offer' && canConfirmSas(action.sas)) {
    return { ...state, step: 'reveal', sas: action.sas, error: null };
  }
  if (action.type === 'reveal' && ['reveal', 'accept'].includes(state.step) && canConfirmSas(action.sas)) {
    return { ...state, step: 'sas', sas: action.sas, error: null };
  }
  if (action.type === 'confirm' && state.step === 'sas' && state.sas && canConfirmSas(state.sas)) {
    return { ...state, step: 'complete', sas: null, error: null };
  }
  return state;
}

export function secondsRemaining(expiresAt: number | null, now: number): number {
  return expiresAt === null || !Number.isFinite(expiresAt) ? 0 : Math.max(0, Math.ceil((expiresAt - now) / 1000));
}

export type JoiningStep =
  | 'paste' | 'host' | 'metadata' | 'redeem' | 'approval' | 'scope'
  | 'scope_confirmed' | 'consent' | 'activation' | 'open' | 'cancelled' | 'expired';
const joiningTransitions: Record<JoiningStep, JoiningStep[]> = {
  paste: ['host', 'metadata', 'cancelled', 'expired'],
  host: ['metadata', 'cancelled', 'expired'],
  metadata: ['redeem', 'cancelled', 'expired'],
  redeem: ['approval', 'cancelled', 'expired'],
  approval: ['scope', 'cancelled', 'expired'],
  scope: ['scope_confirmed', 'cancelled', 'expired'],
  scope_confirmed: ['consent', 'cancelled', 'expired'],
  consent: ['activation', 'cancelled', 'expired'],
  activation: ['open', 'cancelled', 'expired'],
  open: [], cancelled: [], expired: [],
};
export function canAdvanceJoining(from: JoiningStep, to: JoiningStep): boolean {
  return joiningTransitions[from].includes(to);
}
export function canGiveConsent(step: JoiningStep, scopeConfirmed: boolean): boolean {
  return step === 'scope_confirmed' && scopeConfirmed;
}

/** Sequential, abortable polling: no overlaps, no callbacks after cleanup, bounded backoff. */
export function startJoiningPoll<T>(options: {
  read: (signal: AbortSignal) => Promise<T>;
  onValue: (value: T) => boolean;
  onError: (error: unknown) => void;
  schedule?: (callback: () => void, delay: number) => ReturnType<typeof setTimeout>;
  cancel?: (timer: ReturnType<typeof setTimeout>) => void;
}): () => void {
  const controller = new AbortController();
  const schedule = options.schedule ?? setTimeout;
  const cancel = options.cancel ?? clearTimeout;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let attempt = 0;
  const tick = async () => {
    try {
      const value = await options.read(controller.signal);
      if (controller.signal.aborted || options.onValue(value)) return;
      timer = schedule(() => { void tick(); }, Math.min(1000 * 2 ** attempt++, 15000));
    } catch (error) {
      if (!controller.signal.aborted) options.onError(error);
    }
  };
  void tick();
  return () => {
    controller.abort();
    if (timer !== undefined) cancel(timer);
  };
}
