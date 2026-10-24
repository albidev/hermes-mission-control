export type PairingWizardStep = 'start' | 'offer' | 'accept' | 'reveal' | 'sas' | 'complete' | 'cancelled' | 'expired';
export type PairingWizardState = { step: PairingWizardStep; id: string | null; expiresAt: number | null; sas: string | null; error: string | null };
export const initialPairingWizardState: PairingWizardState = { step: 'start', id: null, expiresAt: null, sas: null, error: null };
export type PairingWizardAction = { type: 'start_offer'; id: string; expiresAt: number } | { type: 'import_offer'; id: string; expiresAt: number } | { type: 'accept'; sas: string } | { type: 'reveal'; sas: string } | { type: 'confirm' } | { type: 'cancel' } | { type: 'expire' } | { type: 'error'; code: string };
export function pairingWizardReducer(state: PairingWizardState, action: PairingWizardAction): PairingWizardState {
  if (action.type === 'cancel') return { ...state, step: 'cancelled' };
  if (action.type === 'expire') return { ...state, step: 'expired' };
  if (action.type === 'error') return { ...state, error: action.code };
  if (action.type === 'start_offer') return { step: 'offer', id: action.id, expiresAt: action.expiresAt, sas: null, error: null };
  if (action.type === 'import_offer') return { step: 'accept', id: action.id, expiresAt: action.expiresAt, sas: null, error: null };
  if (action.type === 'accept' && (state.step === 'offer' || state.step === 'accept')) return { ...state, step: 'reveal', sas: action.sas, error: null };
  if (action.type === 'reveal' && (state.step === 'reveal' || state.step === 'accept')) return { ...state, step: 'sas', sas: action.sas, error: null };
  if (action.type === 'confirm' && state.step === 'sas') return { ...state, step: 'complete', error: null };
  return state;
}
export function canConfirmSas(value: string): boolean { return /^\d{6}$/.test(value); }

export type JoiningStep = 'paste' | 'host' | 'metadata' | 'redeem' | 'approval' | 'scope' | 'scope_confirmed' | 'consent' | 'activation' | 'open' | 'cancelled' | 'expired';
const joiningTransitions: Record<JoiningStep, JoiningStep[]> = { paste: ['host', 'metadata', 'cancelled', 'expired'], host: ['metadata', 'cancelled', 'expired'], metadata: ['redeem', 'cancelled', 'expired'], redeem: ['approval', 'cancelled', 'expired'], approval: ['scope', 'cancelled', 'expired'], scope: ['scope_confirmed', 'cancelled', 'expired'], scope_confirmed: ['consent', 'cancelled', 'expired'], consent: ['activation', 'cancelled', 'expired'], activation: ['open', 'cancelled', 'expired'], open: [], cancelled: [], expired: [] };
export function canAdvanceJoining(from: JoiningStep, to: JoiningStep): boolean { return joiningTransitions[from].includes(to); }
export function canGiveConsent(step: JoiningStep, scopeConfirmed: boolean): boolean { return step === 'scope_confirmed' && scopeConfirmed; }
