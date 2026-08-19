import { useCallback, useEffect, useState } from 'react';
import { CheckCircle2, XCircle, RefreshCw, Inbox, ShieldCheck } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import {
  loadMissionControlCandidates,
  approveCandidate,
  rejectCandidate,
  type MissionControlCandidate,
} from '../lib/hermes-api';
import { useMissionControl } from '../lib/mission-control-store';

const STATUS_COLORS: Record<string, string> = {
  pending: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
  approved: 'bg-sky-500/10 text-sky-400 border-sky-500/30',
  rejected: 'bg-red-500/10 text-red-400 border-red-500/30',
  promoted: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
};

function statusBadge(status: string) {
  const cls = STATUS_COLORS[status] ?? 'bg-surface text-foreground/70 border-border';
  return <Badge className={`${cls} border`}>{status}</Badge>;
}

export function ApprovalsRoute() {
  const { storedToken } = useMissionControl();
  const [candidates, setCandidates] = useState<MissionControlCandidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [rejectingId, setRejectingId] = useState<string | null>(null);
  const [rejectReason, setRejectReason] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const snap = await loadMissionControlCandidates(storedToken ?? undefined);
      setCandidates(snap.candidates);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load candidates');
    } finally {
      setLoading(false);
    }
  }, [storedToken]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleApprove = async (c: MissionControlCandidate) => {
    try {
      await approveCandidate(storedToken ?? undefined, c.id);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Approve failed');
    }
  };

  const handleReject = async (c: MissionControlCandidate) => {
    const reason = rejectReason[c.id] ?? '';
    try {
      await rejectCandidate(storedToken ?? undefined, c.id, reason);
      setRejectReason((prev) => ({ ...prev, [c.id]: '' }));
      setRejectingId(null);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Reject failed');
    }
  };

  const pending = candidates.filter((c) => c.status === 'pending');
  const reviewed = candidates.filter((c) => c.status !== 'pending');

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-foreground">Approvals</h1>
          <p className="text-sm text-foreground/60">
            Nightly brain candidates. Approve to enter quarantine (1 day), or reject with an
            optional reason used as model feedback.
          </p>
        </div>
        <Button variant="secondary" onClick={() => void refresh()} disabled={loading}>
          <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </Button>
      </div>

      {error && (
        <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {loading && pending.length === 0 && reviewed.length === 0 ? (
        <div className="flex items-center justify-center py-16 text-foreground/50">
          <RefreshCw className="h-5 w-5 animate-spin" />
          <span className="ml-2">Loading candidates…</span>
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
                No candidates awaiting approval.
              </Card>
            ) : (
              <div className="space-y-3">
                {pending.map((c) => (
                  <Card key={c.id} className="p-4">
                    <div className="flex items-start justify-between gap-4">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <h3 className="truncate font-semibold text-foreground">{c.title}</h3>
                          {statusBadge(c.status)}
                        </div>
                        <p className="mt-1 text-xs text-foreground/50">
                          {c.type} · {c.created} · {c._filename}
                        </p>
                        <pre className="mt-2 max-h-40 overflow-auto rounded bg-surface p-2 text-xs text-foreground/70">
                          {c.body}
                        </pre>
                      </div>
                      <div className="flex shrink-0 flex-col items-end gap-2">
                        <Button size="sm" onClick={() => void handleApprove(c)}>
                          <CheckCircle2 className="h-4 w-4" />
                          Approve
                        </Button>
                        {rejectingId === c.id ? (
                          <div className="flex flex-col items-end gap-1">
                            <input
                              className="w-56 rounded border border-border bg-surface px-2 py-1 text-xs text-foreground"
                              placeholder="Reason (optional)"
                              value={rejectReason[c.id] ?? ''}
                              onChange={(e) =>
                                setRejectReason((prev) => ({ ...prev, [c.id]: e.target.value }))
                              }
                            />
                            <div className="flex gap-1">
                              <Button size="sm" variant="danger" onClick={() => void handleReject(c)}>
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
                  <Card key={c.id} className="p-3">
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
                            <pre className="mt-2 max-h-64 overflow-auto rounded bg-surface p-2 text-xs text-foreground/70">
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
        </>
      )}
    </div>
  );
}
