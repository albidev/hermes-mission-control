import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Clock3,
  Eye,
  Pause,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Timer,
  Trash2,
  X,
} from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { Modal } from '../components/Modal';
import { PullToReloadIndicator } from '../components/PullToReloadIndicator';
import { usePullToReload } from '../hooks/usePullToReload';
import { useI18n } from '../lib/i18n';
import { useMissionControl } from '../lib/mission-control-store';
import { recordReloadDiagnostic } from '../lib/reload-diagnostics';
import {
  createMissionControlCronJob,
  deleteMissionControlCronJob,
  loadMissionControlCronJob,
  loadMissionControlCronJobs,
  pauseMissionControlCronJob,
  resumeMissionControlCronJob,
  runMissionControlCronJob,
  updateMissionControlCronJob,
  type MissionControlCronJob,
} from '../lib/hermes-api';

function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? value : new Date(parsed).toLocaleString();
}

function formatRelative(value: string | null | undefined): string {
  if (!value) return '—';
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return value;
  const delta = parsed - Date.now();
  const minutes = Math.round(Math.abs(delta) / 60000);
  if (minutes < 1) return 'now';
  if (minutes < 60) return `${minutes}m ${delta >= 0 ? 'from now' : 'ago'}`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ${delta >= 0 ? 'from now' : 'ago'}`;
  const days = Math.round(hours / 24);
  return `${days}d ${delta >= 0 ? 'from now' : 'ago'}`;
}

function isCronPaused(job: MissionControlCronJob): boolean {
  return !job.enabled || job.state === 'paused';
}

function statusVariant(job: MissionControlCronJob): 'positive' | 'warning' | 'negative' | 'default' {
  if (job.lastError || job.lastStatus === 'error' || job.lastStatus === 'failed') return 'negative';
  if (isCronPaused(job)) return 'warning';
  if (job.state === 'scheduled' || job.state === 'running') return 'positive';
  return 'default';
}

function statusLabel(job: MissionControlCronJob, t: (key: string) => string): string {
  if (job.lastError || job.lastStatus === 'error' || job.lastStatus === 'failed') return t('cron.status.error');
  if (isCronPaused(job)) return t('cron.status.paused');
  if (job.noAgent) return t('cron.status.script');
  if (job.state === 'completed') return t('cron.status.completed');
  return t('cron.status.scheduled');
}

type CronFormState = {
  name: string;
  prompt: string;
  schedule: string;
  deliver: string;
  repeat: string;
  script: string;
  noAgent: boolean;
  skills: string;
  enabledToolsets: string;
  workdir: string;
  monitorScript: string;
  monitorUrl: string;
  reasoningEffort: string;
};

const emptyForm: CronFormState = {
  name: '',
  prompt: '',
  schedule: 'every 1h',
  deliver: 'local',
  repeat: '',
  script: '',
  noAgent: false,
  skills: '',
  enabledToolsets: '',
  workdir: '',
  monitorScript: '',
  monitorUrl: '',
  reasoningEffort: '',
};

function formFromJob(job: MissionControlCronJob): CronFormState {
  const repeat = typeof job.repeat === 'number'
    ? String(job.repeat)
    : job.repeat?.times ? String(job.repeat.times) : '';
  return {
    name: job.label,
    prompt: job.prompt,
    schedule: job.scheduleExpr || job.scheduleDisplay,
    deliver: job.deliver || 'local',
    repeat,
    script: job.script || '',
    noAgent: Boolean(job.noAgent),
    skills: job.skills.join(', '),
    enabledToolsets: (job.enabledToolsets || []).join(', '),
    workdir: job.workdir || '',
    monitorScript: job.monitorScript || '',
    monitorUrl: job.monitorUrl || '',
    reasoningEffort: job.reasoningEffort || '',
  };
}

function toPayload(form: CronFormState): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    name: form.name.trim() || undefined,
    prompt: form.prompt,
    schedule: form.schedule.trim(),
    deliver: form.deliver.trim() || undefined,
    repeat: form.repeat.trim() ? Number(form.repeat) : undefined,
    script: form.script.trim() || undefined,
    no_agent: form.noAgent,
    skills: form.skills.split(',').map((value) => value.trim()).filter(Boolean),
    enabled_toolsets: form.enabledToolsets.split(',').map((value) => value.trim()).filter(Boolean),
    workdir: form.workdir.trim() || undefined,
    monitor_script: form.monitorScript.trim() || undefined,
    monitor_url: form.monitorUrl.trim() || undefined,
    reasoning_effort: form.reasoningEffort.trim() || undefined,
  };
  return Object.fromEntries(Object.entries(payload).filter(([, value]) => value !== undefined));
}

function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: React.ReactNode;
  hint?: string;
}) {
  return (
    <label className="flex flex-col gap-1.5 text-sm text-text">
      <span className="font-medium">{label}</span>
      {children}
      {hint ? <span className="text-xs text-text-subtle">{hint}</span> : null}
    </label>
  );
}

function CronFormModal({
  job,
  onClose,
  onSaved,
}: {
  job: MissionControlCronJob | null;
  onClose: () => void;
  onSaved: (job: MissionControlCronJob) => void;
}) {
  const { storedToken } = useMissionControl();
  const { t } = useI18n();
  const [form, setForm] = useState<CronFormState>(() => job ? formFromJob(job) : emptyForm);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const update = (key: keyof CronFormState, value: string | boolean) => {
    setForm((previous) => ({ ...previous, [key]: value }));
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    if (!form.schedule.trim()) {
      setError(t('cron.form.scheduleRequired'));
      return;
    }
    if (form.noAgent && !form.script.trim()) {
      setError(t('cron.form.scriptRequired'));
      return;
    }
    setSaving(true);
    try {
      const saved = job
        ? await updateMissionControlCronJob(job.id, toPayload(form), storedToken || undefined)
        : await createMissionControlCronJob(toPayload(form), storedToken || undefined);
      onSaved(saved);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('cron.form.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open
      title={job ? t('cron.form.editTitle') : t('cron.form.createTitle')}
      subtitle={job ? job.label : t('cron.form.createSubtitle')}
      onClose={onClose}
      footer={
        <div className="flex items-center justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={onClose}>{t('common.cancel')}</Button>
          <Button size="sm" variant="primary" loading={saving} onClick={submit}>
            {job ? t('cron.actions.save') : t('cron.actions.create')}
          </Button>
        </div>
      }
    >
      <form className="grid gap-4" onSubmit={submit}>
        {error ? <div className="rounded-lg border border-negative/30 bg-negative-subtle px-3 py-2 text-sm text-negative">{error}</div> : null}
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t('cron.form.name')}>
            <input className="mc-input" value={form.name} onChange={(event) => update('name', event.target.value)} placeholder={t('cron.form.namePlaceholder')} />
          </Field>
          <Field label={t('cron.form.schedule')} hint={t('cron.form.scheduleHint')}>
            <input className="mc-input font-mono" value={form.schedule} onChange={(event) => update('schedule', event.target.value)} placeholder="0 9 * * *" required />
          </Field>
        </div>
        <Field label={t('cron.form.prompt')} hint={t('cron.form.promptHint')}>
          <textarea className="mc-input min-h-32 resize-y" value={form.prompt} onChange={(event) => update('prompt', event.target.value)} placeholder={t('cron.form.promptPlaceholder')} />
        </Field>
        <div className="grid gap-4 sm:grid-cols-3">
          <Field label={t('cron.form.delivery')}>
            <input className="mc-input" value={form.deliver} onChange={(event) => update('deliver', event.target.value)} placeholder="local / discord:..." />
          </Field>
          <Field label={t('cron.form.repeat')} hint={t('cron.form.repeatHint')}>
            <input className="mc-input" type="number" min="1" value={form.repeat} onChange={(event) => update('repeat', event.target.value)} placeholder={t('cron.form.forever')} />
          </Field>
          <Field label={t('cron.form.reasoning')}>
            <select className="mc-input" value={form.reasoningEffort} onChange={(event) => update('reasoningEffort', event.target.value)}>
              <option value="">{t('cron.form.default')}</option>
              {['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'].map((effort) => <option key={effort} value={effort}>{effort}</option>)}
            </select>
          </Field>
        </div>
        <div className="flex items-center gap-3 rounded-lg border border-border-subtle bg-surface-raised/30 px-3 py-2">
          <input id="cron-no-agent" type="checkbox" checked={form.noAgent} onChange={(event) => update('noAgent', event.target.checked)} />
          <label htmlFor="cron-no-agent" className="text-sm text-text">{t('cron.form.noAgent')}</label>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t('cron.form.script')} hint={t('cron.form.scriptHint')}>
            <input className="mc-input font-mono" value={form.script} onChange={(event) => update('script', event.target.value)} placeholder="script-name.sh" />
          </Field>
          <Field label={t('cron.form.workdir')}>
            <input className="mc-input font-mono" value={form.workdir} onChange={(event) => update('workdir', event.target.value)} placeholder="/path/to/project" />
          </Field>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t('cron.form.skills')} hint={t('cron.form.commaHint')}>
            <input className="mc-input" value={form.skills} onChange={(event) => update('skills', event.target.value)} placeholder="skill-a, skill-b" />
          </Field>
          <Field label={t('cron.form.toolsets')} hint={t('cron.form.commaHint')}>
            <input className="mc-input" value={form.enabledToolsets} onChange={(event) => update('enabledToolsets', event.target.value)} placeholder="web, file" />
          </Field>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t('cron.form.monitorScript')}>
            <input className="mc-input font-mono" value={form.monitorScript} onChange={(event) => update('monitorScript', event.target.value)} />
          </Field>
          <Field label={t('cron.form.monitorUrl')}>
            <input className="mc-input" value={form.monitorUrl} onChange={(event) => update('monitorUrl', event.target.value)} placeholder="https://..." />
          </Field>
        </div>
      </form>
    </Modal>
  );
}

function CronDetailModal({ job, onClose }: { job: MissionControlCronJob; onClose: () => void }) {
  const { t } = useI18n();
  const repeat = typeof job.repeat === 'number' ? String(job.repeat) : job.repeat?.times ? `${job.repeat.completed || 0}/${job.repeat.times}` : t('cron.detail.forever');
  return (
    <Modal open title={job.label} subtitle={`${job.scheduleDisplay} · ${job.id}`} onClose={onClose}>
      <div className="flex flex-col gap-5">
        <div className="flex flex-wrap gap-2">
          <Badge variant={statusVariant(job)}>{statusLabel(job, t)}</Badge>
          {job.noAgent ? <Badge variant="accent">{t('cron.status.script')}</Badge> : null}
          {job.scheduleKind === 'once' ? <Badge variant="default">{t('cron.status.oneShot')}</Badge> : null}
        </div>
        <div className="grid gap-3 sm:grid-cols-2 text-sm">
          <div><p className="eyebrow">{t('cron.detail.nextRun')}</p><p className="text-text">{formatDate(job.nextRunAt)} <span className="text-text-subtle">({formatRelative(job.nextRunAt)})</span></p></div>
          <div><p className="eyebrow">{t('cron.detail.lastRun')}</p><p className="text-text">{formatDate(job.lastRunAt)}</p></div>
          <div><p className="eyebrow">{t('cron.detail.delivery')}</p><p className="text-text break-all">{job.deliver || 'local'}</p></div>
          <div><p className="eyebrow">{t('cron.detail.repeat')}</p><p className="text-text">{repeat}</p></div>
          <div><p className="eyebrow">{t('cron.detail.model')}</p><p className="text-text">{job.model}{job.provider ? ` · ${job.provider}` : ''}</p></div>
          <div><p className="eyebrow">{t('cron.detail.created')}</p><p className="text-text">{formatDate(job.createdAt)}</p></div>
        </div>
        <div><p className="eyebrow mb-2">{t('cron.detail.prompt')}</p><pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-lg border border-border-subtle bg-surface-raised/40 p-3 text-xs text-text-muted">{job.prompt || t('cron.detail.noPrompt')}</pre></div>
        {job.script ? <div><p className="eyebrow mb-2">{t('cron.detail.script')}</p><code className="block rounded-lg border border-border-subtle bg-surface-raised/40 p-3 text-xs text-text-muted">{job.script}</code></div> : null}
        {job.skills.length > 0 ? <div><p className="eyebrow mb-2">{t('cron.detail.skills')}</p><div className="flex flex-wrap gap-2">{job.skills.map((skill) => <Badge key={skill}>{skill}</Badge>)}</div></div> : null}
        {(job.lastError || job.lastOutput) ? <div><p className="eyebrow mb-2">{job.lastError ? t('cron.detail.lastError') : t('cron.detail.lastOutput')}</p><pre className={`max-h-72 overflow-auto whitespace-pre-wrap rounded-lg border p-3 text-xs ${job.lastError ? 'border-negative/30 bg-negative-subtle text-negative' : 'border-border-subtle bg-surface-raised/40 text-text-muted'}`}>{job.lastError || job.lastOutput}</pre></div> : null}
      </div>
    </Modal>
  );
}

function Metric({ label, value, icon: Icon }: { label: string; value: string; icon: typeof Timer }) {
  return <Card className="p-4"><div className="flex items-center justify-between text-xs text-text-muted"><span>{label}</span><Icon className="h-4 w-4 text-text-subtle" /></div><p className="mt-2 text-lg font-semibold text-text">{value}</p></Card>;
}

const CRON_POLLING_ENABLED = false; // Temporary regression isolation; do not disable Hermes cron jobs.

export function CronRoute() {
  const { storedToken } = useMissionControl();
  const { t } = useI18n();
  const [jobs, setJobs] = useState<MissionControlCronJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedJob, setSelectedJob] = useState<MissionControlCronJob | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [editingJob, setEditingJob] = useState<MissionControlCronJob | null | undefined>(undefined);
  const [actionJobId, setActionJobId] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  const refresh = useCallback(async (silent = false) => {
    const startedAt = performance.now();
    recordReloadDiagnostic('cron-refresh-start', { silent });
    if (!silent) setRefreshing(true);
    try {
      const next = await loadMissionControlCronJobs(storedToken || undefined);
      setJobs(next);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('cron.loadFailed'));
    } finally {
      recordReloadDiagnostic('cron-refresh-end', { silent, durationMs: Math.round(performance.now() - startedAt) });
      setLoading(false);
      setRefreshing(false);
    }
  }, [storedToken, t]);

  useEffect(() => {
    void refresh();
    if (!CRON_POLLING_ENABLED) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refresh(true);
    }, 30_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const { state: pullState } = usePullToReload({ containerRef, onReload: () => refresh() });

  const counters = useMemo(() => ({
    enabled: jobs.filter((job) => job.enabled && job.state !== 'paused').length,
    paused: jobs.filter((job) => !job.enabled || job.state === 'paused').length,
    failed: jobs.filter((job) => Boolean(job.lastError) || job.lastStatus === 'failed' || job.lastStatus === 'error').length,
  }), [jobs]);

  const orderedJobs = useMemo(
    () => [...jobs].sort((left, right) => Number(isCronPaused(left)) - Number(isCronPaused(right))),
    [jobs],
  );

  const runAction = async (job: MissionControlCronJob, action: 'run' | 'pause' | 'resume' | 'delete') => {
    if (action === 'delete' && !window.confirm(t('cron.deleteConfirm', { name: job.label }))) return;
    setActionJobId(job.id);
    try {
      if (action === 'run') await runMissionControlCronJob(job.id, storedToken || undefined);
      if (action === 'pause') await pauseMissionControlCronJob(job.id, storedToken || undefined);
      if (action === 'resume') await resumeMissionControlCronJob(job.id, storedToken || undefined);
      if (action === 'delete') await deleteMissionControlCronJob(job.id, storedToken || undefined);
      await refresh(true);
      if (selectedJob?.id === job.id && action === 'delete') setSelectedJob(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('cron.actionFailed'));
    } finally {
      setActionJobId(null);
    }
  };

  const openDetail = async (job: MissionControlCronJob) => {
    setDetailLoading(true);
    try {
      setSelectedJob(await loadMissionControlCronJob(job.id, storedToken || undefined));
    } catch {
      setSelectedJob(job);
    } finally {
      setDetailLoading(false);
    }
  };

  const saveJob = (job: MissionControlCronJob) => {
    setEditingJob(undefined);
    setSelectedJob(null);
    setJobs((previous) => {
      const found = previous.some((item) => item.id === job.id);
      return found ? previous.map((item) => item.id === job.id ? job : item) : [job, ...previous];
    });
    void refresh(true);
  };

  return (
    <div ref={containerRef} className="flex h-full flex-col gap-6 overflow-y-auto">
      <PullToReloadIndicator state={pullState} />
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div><span className="eyebrow">{t('cron.eyebrow')}</span><h2 className="mt-1 text-xl font-semibold text-text">{t('cron.title')}</h2><p className="mt-1 text-sm text-text-muted">{t('cron.subtitle')}</p></div>
        <div className="flex gap-2">
          <Button size="sm" variant="ghost" loading={refreshing} icon={<RefreshCw className="h-4 w-4" />} onClick={() => refresh()}>{t('common.refresh')}</Button>
          <Button size="sm" variant="primary" icon={<Plus className="h-4 w-4" />} onClick={() => setEditingJob(null)}>{t('cron.actions.new')}</Button>
        </div>
      </div>
      {error ? <div className="flex items-center justify-between gap-3 rounded-lg border border-negative/30 bg-negative-subtle px-3 py-2 text-sm text-negative"><span>{error}</span><button type="button" onClick={() => setError(null)} aria-label={t('common.dismiss')}><X className="h-4 w-4" /></button></div> : null}
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4"><Metric label={t('cron.metrics.total')} value={String(jobs.length)} icon={Clock3} /><Metric label={t('cron.metrics.enabled')} value={String(counters.enabled)} icon={Play} /><Metric label={t('cron.metrics.paused')} value={String(counters.paused)} icon={Pause} /><Metric label={t('cron.metrics.failed')} value={String(counters.failed)} icon={RotateCcw} /></div>
      <Card padding="none">
        <div className="border-b border-border-subtle px-4 pb-3 pt-4"><span className="eyebrow">{t('cron.list.eyebrow')}</span><h3 className="mt-0.5 text-sm font-semibold text-text">{t('cron.list.title')}</h3></div>
        {loading ? <div className="px-4 py-10 text-center text-sm text-text-muted">{t('cron.loading')}</div> : jobs.length === 0 ? <div className="px-4 py-10 text-center text-sm text-text-muted">{t('cron.empty')}</div> : <div className="divide-y divide-border-subtle">{orderedJobs.map((job) => {
          const busy = actionJobId === job.id;
          const paused = isCronPaused(job);
          return <div key={job.id} className="flex flex-col gap-3 px-4 py-4 xl:flex-row xl:items-center xl:gap-5">
            <div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><h4 className="truncate text-sm font-medium text-text">{job.label}</h4><Badge variant={statusVariant(job)}>{statusLabel(job, t)}</Badge></div><p className="mt-1 truncate font-mono text-xs text-text-muted">{job.scheduleDisplay}</p><p className="mt-1 text-xs text-text-subtle">{job.id}</p></div>
            <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-text-muted sm:grid-cols-4 xl:w-[30rem]"><div><span className="block text-text-subtle">{t('cron.list.next')}</span><span>{formatRelative(job.nextRunAt)}</span></div><div><span className="block text-text-subtle">{t('cron.list.last')}</span><span>{formatRelative(job.lastRunAt)}</span></div><div><span className="block text-text-subtle">{t('cron.list.delivery')}</span><span className="max-w-28 truncate block">{job.deliver || 'local'}</span></div><div><span className="block text-text-subtle">{t('cron.list.mode')}</span><span>{job.noAgent ? t('cron.status.script') : t('cron.status.agent')}</span></div></div>
            <div className="flex flex-wrap items-center gap-1.5 xl:justify-end"><Button iconOnly size="sm" variant="ghost" title={t('cron.actions.detail')} aria-label={t('cron.actions.detail')} onClick={() => openDetail(job)}>{detailLoading && selectedJob?.id === job.id ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Eye className="h-4 w-4" />}</Button><Button iconOnly size="sm" variant="ghost" title={t('cron.actions.edit')} aria-label={t('cron.actions.edit')} onClick={() => setEditingJob(job)}><Pencil className="h-4 w-4" /></Button><Button iconOnly size="sm" variant="ghost" title={t('cron.actions.run')} aria-label={t('cron.actions.run')} loading={busy} onClick={() => runAction(job, 'run')}><Play className="h-4 w-4" /></Button><Button iconOnly size="sm" variant="ghost" title={paused ? t('cron.actions.resume') : t('cron.actions.pause')} aria-label={paused ? t('cron.actions.resume') : t('cron.actions.pause')} loading={busy} onClick={() => runAction(job, paused ? 'resume' : 'pause')}>{paused ? <RotateCcw className="h-4 w-4" /> : <Pause className="h-4 w-4" />}</Button><Button iconOnly size="sm" variant="danger" title={t('cron.actions.delete')} aria-label={t('cron.actions.delete')} loading={busy} onClick={() => runAction(job, 'delete')}><Trash2 className="h-4 w-4" /></Button></div>
          </div>;
        })}</div>}
      </Card>
      {selectedJob ? <CronDetailModal job={selectedJob} onClose={() => setSelectedJob(null)} /> : null}
      {editingJob !== undefined ? <CronFormModal job={editingJob} onClose={() => setEditingJob(undefined)} onSaved={saveJob} /> : null}
    </div>
  );
}
