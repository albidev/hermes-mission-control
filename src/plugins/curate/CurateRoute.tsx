import { useI18n } from '../../lib/i18n';
import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { CheckCircle2, XCircle, RefreshCw, Inbox, ShieldCheck, History, RotateCcw } from 'lucide-react';
import { Card } from '../../components/ui/Card';
import { Badge } from '../../components/ui/Badge';
import { Button } from '../../components/ui/Button';
import {
  loadMissionControlCandidates,
  approveCandidate,
  rejectCandidate,
  loadMissionControlVaults,
  type MissionControlCandidate,
  type MissionControlVaultInfo,
  type MissionControlSynthesisActivity,
  type MissionControlSynthesisOperation,
  loadMissionControlSynthesisActivity,
  revertMissionControlSynthesis,
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

function vaultModeLabel(vault: MissionControlVaultInfo) {
  switch (vault.mode) {
    case 'candidates':
      return 'Candidate queue';
    case 'review_only':
      return 'Review only';
    case 'read_only':
      return 'Read-only';
    default:
      return 'Storage only';
  }
}

function statusBadge(status: string) {
  const cls = STATUS_COLORS[status] ?? 'bg-surface text-foreground/70 border-border';
  return <Badge className={`${cls} border`}>{status}</Badge>;
}

export function CurateRoute() {
  const { t } = useI18n();
  const { storedToken } = useMissionControl();
  const [searchParams, setSearchParams] = useSearchParams();
  const [candidates, setCandidates] = useState<MissionControlCandidate[]>([]);
  const [vaults, setVaults] = useState<MissionControlVaultInfo[]>([]);
  const [vault, setVault] = useState<string>(searchParams.get('vault') || 'core');
  const [loading, setLoading] = useState(true);
  const [rejectingId, setRejectingId] = useState<string | null>(null);
  const [rejectReason, setRejectReason] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [activities, setActivities] = useState<MissionControlSynthesisActivity[]>([]);
  const [activityLoading, setActivityLoading] = useState(false);
  const [activityError, setActivityError] = useState<string | null>(null);
  const [revertingOperation, setRevertingOperation] = useState<string | null>(null);

  const refresh = useCallback(async (targetVault?: string) => {
    const v = targetVault ?? vault;
    setLoading(true);
    setError(null);
    try {
      const [snap, vaultsSnap] = await Promise.all([
        loadMissionControlCandidates(storedToken ?? undefined, undefined, v),
        loadMissionControlVaults(storedToken ?? undefined),
      ]);
      const nextVaults = vaultsSnap.length ? vaultsSnap : [{ id: 'core', label: 'Core', candidates_dir: '' }];
      const resolvedVault = nextVaults.some((item) => item.id === v) ? v : nextVaults[0].id;
      if (resolvedVault !== vault) {
        setVault(resolvedVault);
        return;
      }
      setCandidates(snap.candidates);
      setVaults(nextVaults);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load candidates');
    } finally {
      setLoading(false);
    }
  }, [storedToken, vault]);

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refresh]);

  const refreshActivity = useCallback(async () => {
    setActivityLoading(true);
    setActivityError(null);
    try {
      const snap = await loadMissionControlSynthesisActivity(storedToken ?? undefined, vault);
      setActivities(snap.activities);
    } catch (e) {
      setActivities([]);
      setActivityError(e instanceof Error ? e.message : 'Failed to load synthesis activity');
    } finally {
      setActivityLoading(false);
    }
  }, [storedToken, vault]);

  useEffect(() => {
    void refreshActivity();
  }, [refreshActivity]);

  const selectedVault = vaults.find((item) => item.id === vault);
  const canCurate = selectedVault?.candidate_enabled === true && selectedVault.writable !== false;
  const canRevert = selectedVault?.writable !== false && selectedVault?.read_only !== true;

  const handleVaultChange = (v: string) => {
    setVault(v);
    const nextParams = new URLSearchParams(searchParams);
    if (v === 'core') nextParams.delete('vault');
    else nextParams.set('vault', v);
    setSearchParams(nextParams, { replace: true });
  };

  const handleApprove = async (c: MissionControlCandidate) => {
    if (!canCurate) return;
    try {
      await approveCandidate(storedToken ?? undefined, c.id, vault, c._filename);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Approve failed');
    }
  };

  const handleReject = async (c: MissionControlCandidate) => {
    if (!canCurate) return;
    const reason = rejectReason[c.id] ?? '';
    try {
      await rejectCandidate(storedToken ?? undefined, c.id, reason, vault, c._filename);
      setRejectReason((prev) => ({ ...prev, [c.id]: '' }));
      setRejectingId(null);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Reject failed');
    }
  };

  const handleRevert = async (operation: MissionControlSynthesisOperation) => {
    if (!canRevert || operation.status !== 'applied') return;
    const confirmed = window.confirm(
      `Revert ${operation.action} operation ${operation.operation_id.slice(0, 8)} in ${vault}?`,
    );
    if (!confirmed) return;
    setRevertingOperation(operation.operation_id);
    setActivityError(null);
    try {
      await revertMissionControlSynthesis(storedToken ?? undefined, operation.operation_id, vault);
      await Promise.all([refreshActivity(), refresh()]);
    } catch (e) {
      setActivityError(e instanceof Error ? e.message : 'Revert failed');
    } finally {
      setRevertingOperation(null);
    }
  };

  const pending = candidates.filter((c) => c.status === 'pending');
  const reviewed = candidates.filter((c) => c.status !== 'pending');

  return (
    <div className="route-page-scroll space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-foreground">{t('nav.curate')}</h1>
          <p className="text-sm text-foreground/60">
            Nightly brain candidates. Approve to enter quarantine (1 day), or reject with an
            optional reason used as model feedback.
          </p>
        </div>
        <div className="flex items-center gap-3">
          {vaults.length > 1 && (
            <select
              className="rounded border border-border bg-surface px-3 py-2 text-sm text-foreground"
              value={vault}
              onChange={(e) => handleVaultChange(e.target.value)}
            >
              {vaults.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.label} — {vaultModeLabel(v)}
                </option>
              ))}
            </select>
          )}
          <Button variant="secondary" onClick={() => void refresh()} disabled={loading}>
            <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
        </div>
      </div>
      {selectedVault && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-surface/50 px-3 py-2 text-xs text-foreground/60">
          <span className="font-medium text-foreground">{selectedVault.label}</span>
          <Badge className="border border-border bg-surface text-foreground/70">
            {vaultModeLabel(selectedVault)}
          </Badge>
          <span>
            {selectedVault.candidate_count ?? candidates.length} candidates ·{' '}
            {selectedVault.pending_count ?? pending.length} pending
          </span>
        </div>
      )}

      {error && (
        <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {loading && pending.length === 0 && reviewed.length === 0 ? (
        <div className="flex items-center justify-center py-16 text-foreground/50">
          <RefreshCw className="h-5 w-5 animate-spin" />
          <span className="ml-2">{t('curate.loading')}</span>
        </div>
      ) : (
        <>
          <section>
            <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-foreground/50">
              Pending ({pending.length})
            </h2>
            {pending.length === 0 ? (
              <Card className="flex items-center gap-3 p-6 text-foreground/50">
                <Inbox className="h-5 w-5" />
                {canCurate
                  ? 'No candidates awaiting approval.'
                  : `${selectedVault ? vaultModeLabel(selectedVault) : 'Storage only'} vault — no candidate approval queue is configured.`}
              </Card>
            ) : (
              <div className="space-y-3">
                {pending.map((c) => (
                  <Card key={`${c.id}:${c._filename}`} className="p-4">
                    <div className="flex items-start justify-between gap-4">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <h3 className="truncate font-semibold text-foreground">{c.title}</h3>
                          {statusBadge(c.status)}
                        </div>
                        <p className="mt-1 text-xs text-foreground/50">
                          {c.type} · {c.created} · {c._filename}
                        </p>
                        <pre className="mt-2 whitespace-pre-wrap break-words rounded bg-surface p-2 text-xs text-foreground/70">
                          {c.body}
                        </pre>
                      </div>
                      <div className="flex shrink-0 flex-col items-end gap-2">
                        <Button size="sm" onClick={() => void handleApprove(c)} disabled={!canCurate}>
                          <CheckCircle2 className="h-4 w-4" />
                          Approve
                        </Button>
                        {rejectingId === c.id ? (
                          <div className="flex flex-col items-end gap-1">
                            <input
                              className="w-56 rounded border border-border bg-surface px-2 py-1 text-xs text-foreground"
                              placeholder={t('curate.reasonPlaceholder')}
                              value={rejectReason[c.id] ?? ''}
                              onChange={(e) =>
                                setRejectReason((prev) => ({ ...prev, [c.id]: e.target.value }))
                              }
                            />
                            <div className="flex gap-1">
                              <Button size="sm" variant="danger" onClick={() => void handleReject(c)} disabled={!canCurate}>
                                <XCircle className="h-4 w-4" />
                                Reject
                              </Button>
                              <Button size="sm" variant="ghost" onClick={() => setRejectingId(null)}>
                                Cancel
                              </Button>
                            </div>
                          </div>
                        ) : (
                          <Button size="sm" variant="ghost" onClick={() => setRejectingId(c.id)}>
                            <XCircle className="h-4 w-4" />
                            Reject
                          </Button>
                        )}
                      </div>
                    </div>
                  </Card>
                ))}
              </div>
            )}
          </section>

          {reviewed.length > 0 && (
            <section>
              <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-foreground/50">
                Reviewed ({reviewed.length})
              </h2>
              <div className="space-y-2">
                {reviewed.map((c) => (
                  <Card key={`${c.id}:${c._filename}`} className="p-3">
                    <div className="flex items-center justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="truncate font-medium text-foreground">{c.title}</span>
                          {statusBadge(c.status)}
                        </div>
                        {c.status === 'rejected' && c.rejection_reason && (
                          <p className="mt-1 text-xs text-red-400/80">Reason: {c.rejection_reason}</p>
                        )}
                        {c.status === 'approved' && c.quarantine_until && (
                          <p className="mt-1 flex items-center gap-1 text-xs text-sky-400/80">
                            <ShieldCheck className="h-3 w-3" />
                            In quarantine until {new Date(c.quarantine_until).toLocaleString()}
                          </p>
                        )}
                        {c.body && (
                          <details className="group mt-2">
                            <summary className="cursor-pointer text-xs text-foreground/50 hover:text-foreground/80">
                              Show full text
                            </summary>
                            <pre className="mt-2 whitespace-pre-wrap break-words rounded bg-surface p-2 text-xs text-foreground/70">
                              {c.body}
                            </pre>
                          </details>
                        )}
                      </div>
                    </div>
                  </Card>
                ))}
              </div>
            </section>
          )}
          <section>
            <div className="mb-3 flex items-center justify-between">
              <h2 className="flex items-center gap-2 text-sm font-semibold uppercase tracking-wide text-foreground/50">
                <History className="h-4 w-4" />
                Synthesis activity ({activities.length})
              </h2>
              <Button variant="ghost" size="sm" onClick={() => void refreshActivity()} disabled={activityLoading}>
                <RefreshCw className={`h-3.5 w-3.5 ${activityLoading ? 'animate-spin' : ''}`} />
                Refresh activity
              </Button>
            </div>
            {activityError && (
              <div className="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
                {activityError}
              </div>
            )}
            {activityLoading && activities.length === 0 ? (
              <Card className="flex items-center gap-3 p-6 text-foreground/50">
                <RefreshCw className="h-5 w-5 animate-spin" />
                Loading synthesis activity…
              </Card>
            ) : activities.length === 0 ? (
              <Card className="p-6 text-sm text-foreground/50">
                No session_synthesis operations recorded for this vault.
              </Card>
            ) : (
              <div className="space-y-3">
                {activities.map((activity) => (
                  <Card key={activity.synthesis_id} className="p-4">
                    <div className="flex items-start justify-between gap-4">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <h3 className="font-semibold text-foreground">{activity.outcome}</h3>
                          {activity.provider && <Badge className="border border-border bg-surface text-foreground/70">{activity.provider}</Badge>}
                          {activity.model && <span className="font-mono text-xs text-foreground/45">{activity.model}</span>}
                        </div>
                        <p className="mt-1 break-all font-mono text-xs text-foreground/50">
                          synthesis {activity.synthesis_id} · session {activity.session_id ?? '—'}
                        </p>
                        <p className="mt-1 text-xs text-foreground/45">
                          {activity.timestamp ? new Date(activity.timestamp).toLocaleString() : 'Timestamp unavailable'}
                        </p>
                        <div className="mt-3 flex flex-wrap gap-2">
                          {activity.concepts.map((concept) => (
                            <span
                              key={concept.id}
                              className={`rounded border px-2 py-1 font-mono text-xs ${concept.exists ? 'border-emerald-500/20 bg-emerald-500/5 text-emerald-300/80' : 'border-red-500/20 bg-red-500/5 text-red-300/80'}`}
                              title={concept.path}
                            >
                              {concept.title || concept.id}
                            </span>
                          ))}
                        </div>
                      </div>
                    </div>
                    {activity.operations.length > 0 ? (
                      <div className="mt-4 space-y-2 border-t border-border/60 pt-3">
                        {activity.operations.map((operation) => (
                          <div key={operation.operation_id} className="flex flex-wrap items-center justify-between gap-2 rounded bg-surface/60 px-3 py-2 text-xs">
                            <div className="flex min-w-0 flex-wrap items-center gap-2">
                              <Badge className="border border-border bg-surface text-foreground/70">{operation.action}</Badge>
                              {statusBadge(operation.status)}
                              <span className="truncate font-mono text-foreground/50" title={operation.note_path}>{operation.note_path}</span>
                            </div>
                            {operation.status === 'applied' && (
                              <Button
                                size="sm"
                                variant="danger"
                                onClick={() => void handleRevert(operation)}
                                disabled={!canRevert || revertingOperation === operation.operation_id}
                              >
                                <RotateCcw className={`h-3.5 w-3.5 ${revertingOperation === operation.operation_id ? 'animate-spin' : ''}`} />
                                {revertingOperation === operation.operation_id ? 'Reverting…' : 'Revert'}
                              </Button>
                            )}
                          </div>
                        ))}
                      </div>
                    ) : (
                      <p className="mt-3 border-t border-border/60 pt-3 text-xs text-foreground/45">
                        No reversible operation recorded for this synthesis.
                      </p>
                    )}
                  </Card>
                ))}
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
