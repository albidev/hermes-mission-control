import { useI18n } from '../../lib/i18n';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  CheckCircle2,
  ChevronDown,
  History,
  Inbox,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldCheck,
  XCircle,
} from 'lucide-react';
import { Card } from '../../components/ui/Card';
import { Badge } from '../../components/ui/Badge';
import { Button } from '../../components/ui/Button';
import { Modal } from '../../components/Modal';
import { PageHeader } from '../../components/PageHeader';
import {
  approveCandidate,
  applyMissionControlSessionSynthesisCandidate,
  loadMissionControlCandidates,
  loadMissionControlSessionSynthesisCandidates,
  loadMissionControlSynthesisActivity,
  loadMissionControlVaults,
  rejectCandidate,
  rejectMissionControlSessionSynthesisCandidate,
  revertMissionControlSynthesis,
  type MissionControlCandidate,
  type MissionControlSessionSynthesisCandidate,
  type MissionControlSynthesisActivity,
  type MissionControlSynthesisOperation,
  type MissionControlVaultInfo,
} from '../../lib/hermes-api';
import { useMissionControl } from '../../lib/mission-control-store';

const STATUS_COLORS: Record<string, string> = {
  pending: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
  approved: 'bg-sky-500/10 text-sky-400 border-sky-500/30',
  rejected: 'bg-red-500/10 text-red-400 border-red-500/30',
  promoted: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
  applied: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
  reverted: 'bg-slate-500/10 text-slate-400 border-slate-500/30',
  conflict: 'bg-red-500/10 text-red-400 border-red-500/30',
  prepared: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
};

type CurateTab = 'candidates' | 'synthesis' | 'activity' | 'history';
type ActivityOutcome = 'all' | 'created' | 'merged' | 'noop' | 'failed' | 'reverted' | 'conflict';
type SortDirection = 'newest' | 'oldest';

function parseCurateTab(value: string | null): CurateTab {
  return value === 'synthesis' || value === 'activity' || value === 'history' ? value : 'candidates';
}

function vaultModeLabel(vault: MissionControlVaultInfo) {
  switch (vault.mode) {
    case 'candidates': return 'Candidate queue';
    case 'review_only': return 'Review only';
    case 'read_only': return 'Read-only';
    default: return 'Storage only';
  }
}

function statusBadge(status: string) {
  const cls = STATUS_COLORS[status] ?? 'bg-surface text-foreground/70 border-border';
  return <Badge className={`${cls} border`}>{status}</Badge>;
}

function formatActivityTime(value?: string) {
  if (!value) return 'Timestamp unavailable';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function activityHasHistory(activity: MissionControlSynthesisActivity) {
  return activity.outcome === 'reverted' || activity.operations.some((operation) => operation.status === 'reverted' || operation.status === 'conflict');
}

function activityRevertOperation(activity: MissionControlSynthesisActivity) {
  return activity.operations.find((operation) => operation.status === 'applied');
}

function MetricCard({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="min-w-0 rounded-lg bg-surface-sunken/35 p-3 sm:p-4">
      <p className="truncate text-[11px] font-medium uppercase tracking-wide text-text-muted">{label}</p>
      <p className="mt-1 text-xl font-semibold text-text tabular-nums">{value}</p>
      <p className="mt-1 truncate text-[11px] text-text-subtle">{hint}</p>
    </div>
  );
}

function SynthesisRow({
  activity,
  selected,
  canRevert,
  revertingOperation,
  onInspect,
  onRevert,
}: {
  activity: MissionControlSynthesisActivity;
  selected: boolean;
  canRevert: boolean;
  revertingOperation: string | null;
  onInspect: (activity: MissionControlSynthesisActivity) => void;
  onRevert: (operation: MissionControlSynthesisOperation) => void;
}) {
  const revertOperation = activityRevertOperation(activity);
  return (
    <article className={`group border-b border-border-subtle/60 px-3 py-3 transition-colors last:border-b-0 sm:px-4 ${selected ? 'bg-surface-sunken/70 ring-1 ring-inset ring-accent/30' : 'hover:bg-surface-sunken/40'}`}>
      <div className="flex items-start gap-3">
        <div className="mt-1 shrink-0">
          <span className={`block h-2.5 w-2.5 rounded-full ${activity.outcome === 'failed' || activityHasHistory(activity) ? 'bg-warning' : 'bg-positive'}`} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
            <button type="button" onClick={() => onInspect(activity)} className="block min-w-0 max-w-full flex-1 text-left">
              <p className="truncate text-sm font-semibold capitalize text-text hover:text-accent">{activity.outcome}</p>
              <p className="mt-0.5 truncate text-xs text-text-muted">{activity.concepts.length} concept{activity.concepts.length === 1 ? '' : 's'} · {activity.operations.length} operation{activity.operations.length === 1 ? '' : 's'}</p>
            </button>
            {statusBadge(activity.outcome)}
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px] text-text-subtle">
            <span className="rounded-full bg-surface px-2 py-0.5 font-medium text-text-muted">{activity.provider || 'provider unknown'}</span>
            <span className="min-w-0 max-w-[15rem] truncate">{activity.model || 'model unknown'}</span>
            <span>·</span>
            <span title={formatActivityTime(activity.timestamp)}>{formatActivityTime(activity.timestamp)}</span>
          </div>
          <p className="mt-2 truncate font-mono text-[10px] text-text-subtle" title={activity.synthesis_id}>
            synthesis {activity.synthesis_id} · session {activity.session_id || '—'}
          </p>
          <div className="mt-2 flex min-w-0 flex-wrap gap-1.5">
            {activity.concepts.slice(0, 5).map((concept) => (
              <span key={concept.id} className={`max-w-full truncate rounded-full border px-2 py-0.5 text-[11px] ${concept.exists ? 'border-positive/20 bg-positive/5 text-positive' : 'border-negative/20 bg-negative/5 text-negative'}`} title={concept.path}>
                {concept.title || concept.id}
              </span>
            ))}
            {activity.concepts.length > 5 ? <span className="rounded-full bg-surface px-2 py-0.5 text-[11px] text-text-subtle">+{activity.concepts.length - 5}</span> : null}
          </div>
          <div className="mt-3 flex flex-wrap items-center justify-end gap-1.5">
            <Button type="button" size="sm" variant="ghost" className="!min-w-0 !border-0 !bg-transparent !px-2 text-xs text-text-muted hover:!bg-surface-sunken hover:!text-text" onClick={() => onInspect(activity)}>
              View details
            </Button>
            {revertOperation ? (
              <Button type="button" size="sm" variant="danger" className="!min-w-0 !px-2 sm:!px-3" disabled={!canRevert || revertingOperation === revertOperation.operation_id} onClick={() => onRevert(revertOperation)}>
                <RotateCcw size={13} className={revertingOperation === revertOperation.operation_id ? 'animate-spin' : ''} />
                {revertingOperation === revertOperation.operation_id ? 'Reverting…' : 'Revert'}
              </Button>
            ) : null}
          </div>
        </div>
      </div>
    </article>
  );
}

function SynthesisDetails({
  activity,
  canRevert,
  revertingOperation,
  onClose,
  onRevert,
}: {
  activity: MissionControlSynthesisActivity;
  canRevert: boolean;
  revertingOperation: string | null;
  onClose: () => void;
  onRevert: (operation: MissionControlSynthesisOperation) => void;
}) {
  return (
    <Modal
      open
      title={`${activity.outcome} synthesis`}
      subtitle={`${activity.provider || 'provider unknown'} · ${activity.model || 'model unknown'}`}
      onClose={onClose}
      borderless
      footer={<div className="flex flex-wrap gap-2"><span className="self-center text-xs text-text-subtle">Vault-scoped operation</span></div>}
    >
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2">{statusBadge(activity.outcome)}<span className="rounded-full bg-surface-sunken px-2 py-0.5 text-xs text-text-muted">{formatActivityTime(activity.timestamp)}</span></div>
        <dl className="grid grid-cols-1 gap-3 text-xs sm:grid-cols-2">
          <div><dt className="text-text-subtle">Synthesis ID</dt><dd className="mt-1 break-all font-mono text-text">{activity.synthesis_id}</dd></div>
          <div><dt className="text-text-subtle">Session ID</dt><dd className="mt-1 break-all font-mono text-text">{activity.session_id || '—'}</dd></div>
        </dl>
        <div>
          <p className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-text-subtle">Affected concepts</p>
          <div className="space-y-2">
            {activity.concepts.length === 0 ? <p className="text-xs text-text-muted">No concept IDs recorded.</p> : activity.concepts.map((concept) => <div key={concept.id} className="rounded-md bg-surface-sunken/45 px-3 py-2"><div className="flex items-center gap-2">{concept.exists ? <CheckCircle2 size={13} className="text-positive" /> : <XCircle size={13} className="text-negative" />}<span className="text-sm text-text">{concept.title || concept.id}</span></div><p className="mt-1 break-all font-mono text-[10px] text-text-subtle">{concept.path}</p></div>)}
          </div>
        </div>
        <div>
          <p className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-text-subtle">Operations</p>
          <div className="space-y-2">
            {activity.operations.length === 0 ? <p className="text-xs text-text-muted">This historical synthesis has no reversible journal entry.</p> : activity.operations.map((operation) => <div key={operation.operation_id} className="rounded-md bg-surface-sunken/45 px-3 py-2"><div className="flex flex-wrap items-center justify-between gap-2"><div className="flex min-w-0 flex-wrap items-center gap-2">{statusBadge(operation.action)}{statusBadge(operation.status)}<span className="truncate font-mono text-[10px] text-text-subtle">{operation.note_path}</span></div>{operation.status === 'applied' ? <Button type="button" size="sm" variant="danger" disabled={!canRevert || revertingOperation === operation.operation_id} onClick={() => onRevert(operation)}><RotateCcw size={13} />{revertingOperation === operation.operation_id ? 'Reverting…' : 'Revert'}</Button> : null}</div><p className="mt-1 break-all font-mono text-[10px] text-text-subtle">operation {operation.operation_id}</p></div>)}
          </div>
        </div>
      </div>
    </Modal>
  );
}

function CandidateRow({
  candidate,
  canCurate,
  rejecting,
  reason,
  onApprove,
  onRejectStart,
  onReason,
  onReject,
  onCancel,
}: {
  candidate: MissionControlCandidate;
  canCurate: boolean;
  rejecting: boolean;
  reason: string;
  onApprove: () => void;
  onRejectStart: () => void;
  onReason: (value: string) => void;
  onReject: () => void;
  onCancel: () => void;
}) {
  return (
    <article className="border-b border-border-subtle/60 px-3 py-3 last:border-b-0 sm:px-4">
      <div className="flex items-start gap-3">
        <div className="mt-1 shrink-0"><span className="block h-2.5 w-2.5 rounded-full bg-warning" /></div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-start justify-between gap-2"><h3 className="min-w-0 truncate text-sm font-semibold text-text">{candidate.title}</h3>{statusBadge(candidate.status)}</div>
          <p className="mt-1 truncate text-xs text-text-muted">{candidate.type} · {candidate.created} · {candidate._filename}</p>
          {candidate.body ? <details className="group mt-2"><summary className="cursor-pointer text-xs text-text-subtle hover:text-text">Show full text</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-surface-sunken/45 p-3 text-xs text-text-muted">{candidate.body}</pre></details> : null}
          {candidate.status === 'pending' ? <div className="mt-3 flex flex-wrap justify-end gap-1.5">
            <Button type="button" size="sm" onClick={onApprove} disabled={!canCurate}><CheckCircle2 size={13} />Approve</Button>
            {rejecting ? <div className="flex w-full flex-wrap justify-end gap-1.5 sm:w-auto"><input className="min-w-0 flex-1 rounded-md bg-surface px-2 py-1 text-xs text-text outline-none focus:ring-1 focus:ring-accent/40 sm:w-56 sm:flex-none" placeholder="Reason (optional)" value={reason} onChange={(event) => onReason(event.target.value)} /><Button type="button" size="sm" variant="danger" onClick={onReject} disabled={!canCurate}><XCircle size={13} />Reject</Button><Button type="button" size="sm" variant="ghost" onClick={onCancel}>Cancel</Button></div> : <Button type="button" size="sm" variant="ghost" onClick={onRejectStart}><XCircle size={13} />Reject</Button>}
          </div> : null}
        </div>
      </div>
    </article>
  );
}

function SessionSynthesisCandidateRow({
  candidate,
  canCurate,
  applying,
  rejecting,
  reason,
  onApply,
  onRejectStart,
  onReason,
  onReject,
  onCancel,
  onInspect,
}: {
  candidate: MissionControlSessionSynthesisCandidate;
  canCurate: boolean;
  applying: boolean;
  rejecting: boolean;
  reason: string;
  onApply: () => void;
  onRejectStart: () => void;
  onReason: (value: string) => void;
  onReject: () => void;
  onCancel: () => void;
  onInspect: (candidate: MissionControlSessionSynthesisCandidate) => void;
}) {
  const provenance = candidate.safe_provenance;
  const isPending = candidate.status === 'pending_review';
  const isConflict = candidate.status === 'conflict';
  return (
    <article className="border-b border-border-subtle/60 px-3 py-3 last:border-b-0 sm:px-4">
      <div className="flex items-start gap-3">
        <div className="mt-1 shrink-0">
          <span className={`block h-2.5 w-2.5 rounded-full ${isConflict ? 'bg-negative' : isPending ? 'bg-warning' : 'bg-positive'}`} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <h3 className="min-w-0 truncate text-sm font-semibold text-text">{candidate.title || 'Untitled concept'}</h3>
            {statusBadge(candidate.status)}
          </div>
          <p className="mt-1 truncate text-xs text-text-muted">{candidate.source} · {candidate.candidate_id}</p>
          <div className="mt-2 flex justify-end">
            <Button type="button" size="sm" variant="ghost" className="!min-w-0 !border-0 !bg-transparent !px-2 text-xs text-text-muted hover:!bg-surface-sunken hover:!text-text" onClick={() => onInspect(candidate)}>
              View details
            </Button>
          </div>
          <dl className="mt-2 grid grid-cols-1 gap-x-4 gap-y-1 text-[11px] text-text-subtle sm:grid-cols-2">
            <div className="min-w-0"><dt className="inline text-text-muted">synthesis </dt><dd className="inline break-all font-mono">{candidate.synthesis_id}</dd></div>
            <div className="min-w-0"><dt className="inline text-text-muted">session </dt><dd className="inline break-all font-mono">{candidate.session_id || '—'}</dd></div>
            <div className="min-w-0"><dt className="inline text-text-muted">vault </dt><dd className="inline break-all font-mono">{candidate.vault_id || '—'}</dd></div>
            {candidate.accepted_count !== null || candidate.context_only_count !== null ? (
              <div className="min-w-0"><dt className="inline text-text-muted">concepts </dt><dd className="inline">{candidate.accepted_count ?? 0} accepted · {candidate.context_only_count ?? 0} context-only</dd></div>
            ) : null}
          </dl>
          {provenance.session_title || provenance.created_at || provenance.source_ref || provenance.concept_summary ? (
            <div className="mt-2 rounded-md bg-surface-sunken/45 px-3 py-2 text-[11px] text-text-muted">
              {provenance.session_title ? <p className="truncate"><span className="text-text-subtle">Session: </span>{provenance.session_title}</p> : null}
              {provenance.created_at ? <p className="truncate"><span className="text-text-subtle">Created: </span>{formatActivityTime(provenance.created_at)}</p> : null}
              {provenance.source_ref ? <p className="truncate"><span className="text-text-subtle">Source: </span>{provenance.source_ref}</p> : null}
              {provenance.concept_summary ? <p className="mt-1 line-clamp-3 whitespace-pre-wrap break-words"><span className="text-text-subtle">Summary: </span>{provenance.concept_summary}</p> : null}
            </div>
          ) : null}
          {isPending ? (
            <div className="mt-3 flex flex-wrap justify-end gap-1.5">
              <Button type="button" size="sm" onClick={onApply} disabled={!canCurate || applying}>
                <CheckCircle2 size={13} className={applying ? 'animate-spin' : ''} />
                {applying ? 'Applying…' : 'Approve / Apply'}
              </Button>
              {rejecting ? (
                <div className="flex w-full flex-wrap justify-end gap-1.5 sm:w-auto">
                  <input className="min-w-0 flex-1 rounded-md bg-surface px-2 py-1 text-xs text-text outline-none focus:ring-1 focus:ring-accent/40 sm:w-56 sm:flex-none" placeholder="Reason (optional)" value={reason} onChange={(event) => onReason(event.target.value)} />
                  <Button type="button" size="sm" variant="danger" onClick={onReject} disabled={!canCurate}><XCircle size={13} />Reject</Button>
                  <Button type="button" size="sm" variant="ghost" onClick={onCancel}>Cancel</Button>
                </div>
              ) : (
                <Button type="button" size="sm" variant="ghost" onClick={onRejectStart}><XCircle size={13} />Reject</Button>
              )}
            </div>
          ) : isConflict ? (
            <div className="mt-3 flex flex-wrap items-center justify-end gap-1.5">
              <span className="text-xs text-negative">Conflict — review and re-apply if resolved.</span>
            </div>
          ) : null}
        </div>
      </div>
    </article>
  );
}

function SessionSynthesisCandidateDetails({
  candidate,
  onClose,
}: {
  candidate: MissionControlSessionSynthesisCandidate;
  onClose: () => void;
}) {
  const provenance = Object.entries(candidate.provenance ?? {});
  const extra = Object.entries(candidate.extra ?? {});
  return (
    <Modal
      open
      title={candidate.title || 'Session synthesis candidate'}
      subtitle={`${candidate.source} · ${candidate.status}`}
      onClose={onClose}
      borderless
      footer={<span className="text-xs text-text-subtle">Read-only candidate details · approval actions remain in the queue</span>}
    >
      <div className="flex flex-col gap-4">
        <div className="rounded-lg border border-border-subtle bg-surface-sunken/35 p-3">
          <p className="mb-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-text-subtle">Definition</p>
          <p className="whitespace-pre-wrap break-words text-sm leading-6 text-text">{candidate.definition || 'No definition recorded.'}</p>
        </div>
        <dl className="grid grid-cols-1 gap-3 text-xs sm:grid-cols-2">
          {[['Candidate ID', candidate.candidate_id], ['Synthesis ID', candidate.synthesis_id], ['Session ID', candidate.session_id || '—'], ['Vault', candidate.vault_id || '—'], ['Source', candidate.source], ['Confidence', candidate.confidence || '—'], ['Created', candidate.created_at ? formatActivityTime(candidate.created_at) : '—'], ['Accepted turns', candidate.accepted_count ?? '—'], ['Context-only turns', candidate.context_only_count ?? '—']].map(([label, value]) => (
            <div key={String(label)} className="min-w-0 rounded-md bg-surface-sunken/35 px-3 py-2">
              <dt className="text-text-subtle">{label}</dt>
              <dd className="mt-1 break-all font-mono text-text">{String(value)}</dd>
            </div>
          ))}
        </dl>
        {candidate.safe_provenance.session_title || candidate.safe_provenance.source_ref || candidate.safe_provenance.concept_summary ? (
          <div className="rounded-lg border border-border-subtle bg-surface-sunken/35 p-3 text-xs">
            <p className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-text-subtle">Safe provenance</p>
            {candidate.safe_provenance.session_title ? <p><span className="text-text-subtle">Session: </span>{candidate.safe_provenance.session_title}</p> : null}
            {candidate.safe_provenance.source_ref ? <p className="mt-1 break-all"><span className="text-text-subtle">Source: </span>{candidate.safe_provenance.source_ref}</p> : null}
            {candidate.safe_provenance.concept_summary ? <p className="mt-2 whitespace-pre-wrap break-words text-text-muted">{candidate.safe_provenance.concept_summary}</p> : null}
          </div>
        ) : null}
        {provenance.length > 0 ? <details open className="rounded-lg border border-border-subtle bg-surface-sunken/35 p-3">
          <summary className="cursor-pointer text-[10px] font-semibold uppercase tracking-[0.12em] text-text-subtle">Full provenance</summary>
          <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-surface px-3 py-2 text-[11px] text-text-muted">{JSON.stringify(Object.fromEntries(provenance), null, 2)}</pre>
        </details> : null}
        {extra.length > 0 ? <details className="rounded-lg border border-border-subtle bg-surface-sunken/35 p-3">
          <summary className="cursor-pointer text-[10px] font-semibold uppercase tracking-[0.12em] text-text-subtle">Additional metadata</summary>
          <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-surface px-3 py-2 text-[11px] text-text-muted">{JSON.stringify(Object.fromEntries(extra), null, 2)}</pre>
        </details> : null}
      </div>
    </Modal>
  );
}


export function CurateRoute() {
  const { t } = useI18n();
  const { storedToken } = useMissionControl();
  const [searchParams, setSearchParams] = useSearchParams();
  const [candidates, setCandidates] = useState<MissionControlCandidate[]>([]);
  const [vaults, setVaults] = useState<MissionControlVaultInfo[]>([]);
  const [vault, setVault] = useState<string>(searchParams.get('vault') || 'core');
  const [loading, setLoading] = useState(true);
  const [activityLoading, setActivityLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activityError, setActivityError] = useState<string | null>(null);
  const [activities, setActivities] = useState<MissionControlSynthesisActivity[]>([]);
  const [rejectingId, setRejectingId] = useState<string | null>(null);
  const [rejectReason, setRejectReason] = useState<Record<string, string>>({});
  const [selectedActivity, setSelectedActivity] = useState<MissionControlSynthesisActivity | null>(null);
  const [revertingOperation, setRevertingOperation] = useState<string | null>(null);
  const [activityQuery, setActivityQuery] = useState('');
  const [activityOutcome, setActivityOutcome] = useState<ActivityOutcome>('all');
  const [activitySort, setActivitySort] = useState<SortDirection>('newest');
  const [synthesisCandidates, setSynthesisCandidates] = useState<MissionControlSessionSynthesisCandidate[]>([]);
  const [synthesisLoading, setSynthesisLoading] = useState(false);
  const [synthesisError, setSynthesisError] = useState<string | null>(null);
  const [applyingId, setApplyingId] = useState<string | null>(null);
  const [synthesisRejectingId, setSynthesisRejectingId] = useState<string | null>(null);
  const [synthesisRejectReason, setSynthesisRejectReason] = useState<Record<string, string>>({});
  const [selectedSynthesisCandidate, setSelectedSynthesisCandidate] = useState<MissionControlSessionSynthesisCandidate | null>(null);

  const activeTab = parseCurateTab(searchParams.get('tab'));
  const updateTab = (tab: CurateTab) => {
    const next = new URLSearchParams(searchParams);
    if (tab === 'candidates') next.delete('tab');
    else next.set('tab', tab);
    setSearchParams(next, { replace: true });
  };

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [candidateSnapshot, vaultSnapshot] = await Promise.all([
        loadMissionControlCandidates(storedToken ?? undefined, undefined, vault),
        loadMissionControlVaults(storedToken ?? undefined),
      ]);
      setCandidates(candidateSnapshot.candidates);
      setVaults(vaultSnapshot.length ? vaultSnapshot : [{ id: 'core', label: 'Core', candidates_dir: '' }]);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load candidates');
    } finally {
      setLoading(false);
    }
  }, [storedToken, vault]);

  const refreshActivity = useCallback(async () => {
    setActivityLoading(true);
    setActivityError(null);
    try {
      const snapshot = await loadMissionControlSynthesisActivity(storedToken ?? undefined, vault);
      setActivities(snapshot.activities);
    } catch (e) {
      setActivities([]);
      setActivityError(e instanceof Error ? e.message : 'Failed to load synthesis activity');
    } finally {
      setActivityLoading(false);
    }
  }, [storedToken, vault]);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => { void refreshActivity(); }, [refreshActivity]);

  const refreshSynthesis = useCallback(async () => {
    setSynthesisLoading(true);
    setSynthesisError(null);
    try {
      const snapshot = await loadMissionControlSessionSynthesisCandidates(storedToken ?? undefined, vault);
      setSynthesisCandidates(snapshot.candidates);
    } catch (e) {
      setSynthesisCandidates([]);
      setSynthesisError(e instanceof Error ? e.message : 'Failed to load session synthesis candidates');
    } finally {
      setSynthesisLoading(false);
    }
  }, [storedToken, vault]);

  useEffect(() => { void refreshSynthesis(); }, [refreshSynthesis]);

  const selectedVault = vaults.find((item) => item.id === vault);
  const canCurate = selectedVault?.candidate_enabled === true && selectedVault.writable !== false;
  const canRevert = selectedVault?.writable !== false && selectedVault?.read_only !== true;
  const pending = candidates.filter((candidate) => candidate.status === 'pending');
  const reviewed = candidates.filter((candidate) => candidate.status !== 'pending');
  const historyActivities = activities.filter(activityHasHistory);
  const revertibleCount = activities.reduce((count, activity) => count + (activityRevertOperation(activity) ? 1 : 0), 0);

  const visibleActivities = useMemo(() => {
    const query = activityQuery.trim().toLowerCase();
    return activities
      .filter((activity) => activeTab !== 'history' || activityHasHistory(activity))
      .filter((activity) => activityOutcome === 'all' || activity.outcome === activityOutcome || activity.operations.some((operation) => operation.status === activityOutcome))
      .filter((activity) => !query || [activity.synthesis_id, activity.session_id, activity.provider, activity.model, ...activity.concepts.map((concept) => `${concept.title} ${concept.path}`)].filter(Boolean).join(' ').toLowerCase().includes(query))
      .sort((left, right) => {
        const a = new Date(left.timestamp || 0).getTime();
        const b = new Date(right.timestamp || 0).getTime();
        return activitySort === 'newest' ? b - a : a - b;
      });
  }, [activeTab, activities, activityOutcome, activityQuery, activitySort]);

  const handleVaultChange = (nextVault: string) => {
    setVault(nextVault);
    const nextParams = new URLSearchParams(searchParams);
    if (nextVault === 'core') nextParams.delete('vault');
    else nextParams.set('vault', nextVault);
    setSearchParams(nextParams, { replace: true });
  };

  const handleApprove = async (candidate: MissionControlCandidate) => {
    if (!canCurate) return;
    try { await approveCandidate(storedToken ?? undefined, candidate.id, vault, candidate._filename); await refresh(); } catch (e) { setError(e instanceof Error ? e.message : 'Approve failed'); }
  };

  const handleReject = async (candidate: MissionControlCandidate) => {
    if (!canCurate) return;
    try {
      await rejectCandidate(storedToken ?? undefined, candidate.id, rejectReason[candidate.id] ?? '', vault, candidate._filename);
      setRejectReason((current) => ({ ...current, [candidate.id]: '' }));
      setRejectingId(null);
      await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : 'Reject failed'); }
  };

  const handleRevert = async (operation: MissionControlSynthesisOperation) => {
    if (!canRevert || operation.status !== 'applied') return;
    if (!window.confirm(`Revert ${operation.action} operation ${operation.operation_id.slice(0, 8)} in ${vault}?`)) return;
    setRevertingOperation(operation.operation_id);
    setActivityError(null);
    try {
      await revertMissionControlSynthesis(storedToken ?? undefined, operation.operation_id, vault);
      await Promise.all([refreshActivity(), refresh()]);
      setSelectedActivity(null);
    } catch (e) { setActivityError(e instanceof Error ? e.message : 'Revert failed'); }
    finally { setRevertingOperation(null); }
  };

  const handleSynthesisApply = async (candidate: MissionControlSessionSynthesisCandidate) => {
    if (!canCurate) return;
    setApplyingId(candidate.candidate_id);
    setSynthesisError(null);
    try {
      const result = await applyMissionControlSessionSynthesisCandidate(storedToken ?? undefined, candidate.candidate_id, vault);
      if (result.status === 'conflict') {
        setSynthesisError(`Apply conflict for ${candidate.candidate_id}: ${result.reason || 'unresolvable conflict'}`);
      } else if (result.status === 'failed') {
        setSynthesisError(`Apply failed for ${candidate.candidate_id}: ${result.reason || 'BDH reported failure'}`);
      }
      await refreshSynthesis();
    } catch (e) { setSynthesisError(e instanceof Error ? e.message : 'Apply failed'); }
    finally { setApplyingId(null); }
  };

  const handleSynthesisReject = async (candidate: MissionControlSessionSynthesisCandidate) => {
    if (!canCurate) return;
    try {
      await rejectMissionControlSessionSynthesisCandidate(
        storedToken ?? undefined,
        candidate.candidate_id,
        synthesisRejectReason[candidate.candidate_id] ?? '',
        vault,
      );
      setSynthesisRejectReason((current) => ({ ...current, [candidate.candidate_id]: '' }));
      setSynthesisRejectingId(null);
      await refreshSynthesis();
    } catch (e) { setSynthesisError(e instanceof Error ? e.message : 'Reject failed'); }
  };

  return (
    <div className="route-page-scroll flex flex-col gap-4 sm:gap-5">
      <Card padding="none" className="!border-0">
        <PageHeader
          eyebrow={t('nav.curate')}
          title="Neurogenesis curation"
          description="Review persisted session_synthesis work by vault. Revert is post-commit and conflict-aware."
          meta={selectedVault ? `${selectedVault.label} · ${vaultModeLabel(selectedVault)}` : 'Vault loading'}
          actions={<div className="flex flex-wrap items-center justify-end gap-2">{vaults.length > 1 ? <select className="min-w-0 rounded-md bg-surface px-3 py-1.5 text-xs text-text-muted outline-none focus:ring-1 focus:ring-accent/40" value={vault} onChange={(event) => handleVaultChange(event.target.value)} aria-label="Vault"><option value="core">Core</option>{vaults.filter((item) => item.id !== 'core').map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select> : null}<Button type="button" size="sm" variant="ghost" className="!min-w-0 !border-0 !bg-transparent !px-2 text-text-muted hover:!bg-surface-sunken hover:!text-text" onClick={() => void Promise.all([refresh(), refreshActivity(), refreshSynthesis()])} disabled={loading || activityLoading || synthesisLoading} aria-label="Refresh" title="Refresh"><RefreshCw size={14} className={loading || activityLoading || synthesisLoading ? 'animate-spin' : ''} /><span className="hidden sm:inline">Refresh</span></Button></div>}
        />
        <div className="grid grid-cols-2 gap-1.5 p-2 sm:grid-cols-4 sm:gap-3 sm:p-4">
          <MetricCard label="Synthesis" value={String(activities.length)} hint="Recorded in audit" />
          <MetricCard label="Created" value={String(activities.filter((activity) => activity.outcome === 'created').length)} hint="New concepts" />
          <MetricCard label="Merged" value={String(activities.filter((activity) => activity.outcome === 'merged').length)} hint="Existing notes" />
          <MetricCard label="Revertible" value={String(revertibleCount)} hint={`${historyActivities.length} historical changes`} />
        </div>
      </Card>

      {error ? <div className="rounded-lg border border-negative/30 bg-negative/10 p-3 text-sm text-negative">{error}</div> : null}

      <Card padding="none" className="min-w-0 !border-0">
        <div className="border-b border-border-subtle/60 p-3">
          <div role="tablist" aria-label="Curate views" className="flex flex-nowrap gap-1.5 overflow-x-auto pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:flex-wrap sm:overflow-visible sm:pb-0">
            {([['candidates', 'Candidates', pending.length + reviewed.length], ['synthesis', 'Session synthesis', synthesisCandidates.length], ['activity', 'Synthesis activity', activities.length], ['history', 'Reverted / conflicts', historyActivities.length]] as const).map(([tab, label, count]) => <button key={tab} type="button" role="tab" aria-selected={activeTab === tab} aria-controls="curate-tab-panel" onClick={() => updateTab(tab)} className={`shrink-0 whitespace-nowrap rounded-full px-3 py-1.5 text-xs font-medium transition-colors ${activeTab === tab ? 'bg-accent text-white' : 'bg-surface-sunken text-text-muted hover:bg-border-subtle hover:text-text'}`}>{label} <span className="ml-1 opacity-70">{count}</span></button>)}
          </div>
        </div>

        <div id="curate-tab-panel" role="tabpanel" aria-busy={activeTab === 'candidates' ? loading : activeTab === 'synthesis' ? synthesisLoading : activityLoading}>
          {activeTab === 'candidates' ? (
            loading && candidates.length === 0 ? <div className="flex items-center justify-center gap-2 px-4 py-12 text-sm text-text-muted"><RefreshCw size={14} className="animate-spin" />{t('curate.loading')}</div> : <div>
              <div className="flex items-center justify-between bg-surface-sunken/35 px-4 py-2.5"><span className="text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">Pending <span className="ml-1 text-text-subtle">{pending.length}</span></span>{selectedVault && <span className="text-[11px] text-text-subtle">{canCurate ? 'Approval queue enabled' : `${vaultModeLabel(selectedVault)} vault`}</span>}</div>
              {pending.length === 0 ? <div className="flex items-center gap-3 px-4 py-8 text-sm text-text-muted"><Inbox size={17} />No candidates awaiting approval.</div> : pending.map((candidate) => <CandidateRow key={`${candidate.id}:${candidate._filename}`} candidate={candidate} canCurate={canCurate} rejecting={rejectingId === candidate.id} reason={rejectReason[candidate.id] ?? ''} onApprove={() => void handleApprove(candidate)} onRejectStart={() => setRejectingId(candidate.id)} onReason={(value) => setRejectReason((current) => ({ ...current, [candidate.id]: value }))} onReject={() => void handleReject(candidate)} onCancel={() => setRejectingId(null)} />)}
              {reviewed.length > 0 ? <><div className="flex items-center gap-2 bg-surface-sunken/35 px-4 py-2.5"><ChevronDown size={14} className="text-text-subtle" /><span className="text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">Reviewed <span className="ml-1 text-text-subtle">{reviewed.length}</span></span></div>{reviewed.map((candidate) => <CandidateRow key={`${candidate.id}:${candidate._filename}`} candidate={candidate} canCurate={false} rejecting={false} reason="" onApprove={() => undefined} onRejectStart={() => undefined} onReason={() => undefined} onReject={() => undefined} onCancel={() => undefined} />)}</> : null}
            </div>
          ) : activeTab === 'synthesis' ? (
            <div>
              <div className="flex items-center justify-between bg-surface-sunken/35 px-4 py-2.5">
                <span className="text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">Session synthesis candidates <span className="ml-1 text-text-subtle">{synthesisCandidates.length}</span></span>
                {selectedVault && <span className="text-[11px] text-text-subtle">{canCurate ? 'Review gate enabled' : `${vaultModeLabel(selectedVault)} vault`}</span>}
              </div>
              {synthesisError ? <div className="m-3 rounded-lg border border-negative/30 bg-negative/10 p-3 text-sm text-negative">{synthesisError}</div> : null}
              {synthesisLoading && synthesisCandidates.length === 0 ? <div className="flex items-center justify-center gap-2 px-4 py-12 text-sm text-text-muted"><RefreshCw size={14} className="animate-spin" />Loading session synthesis candidates…</div> : synthesisCandidates.length === 0 ? <div className="flex items-center gap-3 px-4 py-8 text-sm text-text-muted"><Inbox size={17} />No session synthesis candidates for this vault.</div> : <div>{synthesisCandidates.map((candidate) => <SessionSynthesisCandidateRow key={candidate.candidate_id} candidate={candidate} canCurate={canCurate} applying={applyingId === candidate.candidate_id} rejecting={synthesisRejectingId === candidate.candidate_id} reason={synthesisRejectReason[candidate.candidate_id] ?? ''} onApply={() => void handleSynthesisApply(candidate)} onRejectStart={() => setSynthesisRejectingId(candidate.candidate_id)} onReason={(value) => setSynthesisRejectReason((current) => ({ ...current, [candidate.candidate_id]: value }))} onReject={() => void handleSynthesisReject(candidate)} onCancel={() => setSynthesisRejectingId(null)} onInspect={setSelectedSynthesisCandidate} />)}</div>}
            </div>
          ) : (
            <div>
              <div className="flex flex-col gap-2 border-b border-border-subtle/60 p-3 md:flex-row md:items-center">
                <label className="relative block min-w-0 md:flex-1"><Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-subtle" /><input value={activityQuery} onChange={(event) => setActivityQuery(event.target.value)} placeholder="Search synthesis, session, concept…" aria-label="Search synthesis activity" className="h-9 w-full min-w-0 rounded-md bg-surface py-0 pl-9 pr-3 text-sm text-text outline-none placeholder:text-text-subtle focus:ring-1 focus:ring-accent/40" /></label>
                <select value={activityOutcome} onChange={(event) => setActivityOutcome(event.target.value as ActivityOutcome)} className="h-9 min-w-0 rounded-md bg-surface px-3 py-0 text-xs text-text-muted outline-none focus:ring-1 focus:ring-accent/40" aria-label="Filter outcome"><option value="all">All outcomes</option><option value="created">Created</option><option value="merged">Merged</option><option value="reverted">Reverted</option><option value="conflict">Conflicts</option><option value="failed">Failed</option></select>
                <select value={activitySort} onChange={(event) => setActivitySort(event.target.value as SortDirection)} className="h-9 min-w-0 rounded-md bg-surface px-3 py-0 text-xs text-text-muted outline-none focus:ring-1 focus:ring-accent/40" aria-label="Sort activity"><option value="newest">Newest first</option><option value="oldest">Oldest first</option></select>
              </div>
              {activityError ? <div className="m-3 rounded-lg border border-negative/30 bg-negative/10 p-3 text-sm text-negative">{activityError}</div> : null}
              {activityLoading && activities.length === 0 ? <div className="flex items-center justify-center gap-2 px-4 py-12 text-sm text-text-muted"><RefreshCw size={14} className="animate-spin" />Loading synthesis activity…</div> : visibleActivities.length === 0 ? <div className="flex items-center gap-3 px-4 py-12 text-sm text-text-muted"><History size={17} />No synthesis activity matches this view.</div> : <div>{visibleActivities.map((activity) => <SynthesisRow key={activity.synthesis_id} activity={activity} selected={selectedActivity?.synthesis_id === activity.synthesis_id} canRevert={canRevert} revertingOperation={revertingOperation} onInspect={setSelectedActivity} onRevert={(operation) => void handleRevert(operation)} />)}</div>}
            </div>
          )}
        </div>
      </Card>

      {selectedActivity ? <SynthesisDetails activity={selectedActivity} canRevert={canRevert} revertingOperation={revertingOperation} onClose={() => setSelectedActivity(null)} onRevert={(operation) => void handleRevert(operation)} /> : null}
      {selectedSynthesisCandidate ? <SessionSynthesisCandidateDetails candidate={selectedSynthesisCandidate} onClose={() => setSelectedSynthesisCandidate(null)} /> : null}
    </div>
  );
}
