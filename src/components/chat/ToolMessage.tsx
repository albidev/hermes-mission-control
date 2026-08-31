import {
  Activity,
  Archive,
  Bot,
  CheckCircle2,
  Clock3,
  ExternalLink,
  FolderOpen,
  Globe2,
  Image as ImageIcon,
  Layers3,
  Loader2,
  MessageSquare,
  Music2,
  Search,
  ServerCog,
  Terminal,
  Timer,
  Wrench,
  XCircle,
  Zap,
  type LucideIcon,
} from 'lucide-react';
import type { ReactNode } from 'react';
import type { ChatMessage } from '../../lib/chat-protocol';

type ToolFamily =
  | 'execution'
  | 'research'
  | 'delegation'
  | 'scheduler'
  | 'context'
  | 'media'
  | 'integration'
  | 'generic';

type FamilyMeta = {
  label: string;
  icon: LucideIcon;
};

const FAMILY_META: Record<ToolFamily, FamilyMeta> = {
  execution: { label: 'Execution', icon: Terminal },
  research: { label: 'Research', icon: Search },
  delegation: { label: 'Delegation', icon: Layers3 },
  scheduler: { label: 'Scheduler', icon: Timer },
  context: { label: 'Context', icon: Archive },
  media: { label: 'Media', icon: ImageIcon },
  integration: { label: 'Integration', icon: Zap },
  generic: { label: 'Tool', icon: Wrench },
};

function normalizedName(name: string | undefined): string {
  return (name || '').trim().toLowerCase().replace(/[-./]/g, '_');
}

export function classifyTool(toolName: string | undefined): ToolFamily {
  const name = normalizedName(toolName);
  if (name === 'todo') return 'generic';
  if (name === 'delegation' || name.includes('delegate')) return 'delegation';
  if (name === 'cronjob' || name === 'cron' || name.includes('scheduler')) return 'scheduler';
  if (name === 'memory' || name === 'context_engine' || name === 'session_search') return 'context';
  if (name === 'web' || name === 'browser' || name === 'x_search' || name.includes('search')) return 'research';
  if (name === 'image_gen' || name === 'video' || name === 'video_gen' || name === 'tts' || name === 'stt' || name === 'vision') return 'media';
  if (name === 'homeassistant' || name === 'spotify' || name === 'discord' || name === 'discord_admin' || name === 'yuanbao') return 'integration';
  if (name === 'terminal' || name === 'file' || name === 'code_execution' || name === 'code' || name === 'patch' || name.includes('terminal')) return 'execution';
  return 'generic';
}

function compactText(value: string, maxLength = 120): string {
  const compact = value.replace(/\s+/g, ' ').trim();
  return compact.length > maxLength ? `${compact.slice(0, maxLength - 1)}…` : compact;
}

function extractExitCode(value: string): string | null {
  const match = value.match(/(?:exit(?:\s+code)?|status)\s*[:=]\s*(-?\d+)/i);
  return match?.[1] ?? null;
}

function extractWorkdir(value: string): string | null {
  const match = value.match(/(?:working\s+directory|workdir|cwd)\s*[:=]\s*([^\n]+)/i);
  return match?.[1] ? compactText(match[1], 80) : null;
}

function extractUrls(value: string): string[] {
  const matches = value.match(/https?:\/\/[^\s<>)\]"']+/g) || [];
  return [...new Set(matches.map((url) => url.replace(/[.,;:]+$/, '')))];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function parseJsonValue(value: string): unknown {
  const text = value.trim();
  if (!text) return null;
  const fenced = text.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/i);
  const candidates = fenced?.[1] ? [fenced[1].trim(), text] : [text];
  for (const candidate of candidates) {
    try {
      return JSON.parse(candidate) as unknown;
    } catch {
      // Delegation output is normally JSON, but older gateways may add a prefix.
    }
  }
  return null;
}

type DelegationTask = { taskIndex: number; goal: string };
type DelegationResult = {
  taskIndex: number;
  status: string;
  summary: string;
  model: string;
  durationSeconds: number | null;
  error: string;
  tokens: number | null;
};

function parseDelegationTasks(value: string): DelegationTask[] {
  const parsed = parseJsonValue(value);
  if (!isRecord(parsed)) return [];
  if (Array.isArray(parsed.tasks)) {
    return parsed.tasks.flatMap((task, index) => {
      if (!isRecord(task) || typeof task.goal !== 'string' || !task.goal.trim()) return [];
      return [{ taskIndex: index, goal: task.goal.trim() }];
    });
  }
  if (typeof parsed.goal === 'string' && parsed.goal.trim()) return [{ taskIndex: 0, goal: parsed.goal.trim() }];
  return [];
}

function parseDelegationResults(value: string): DelegationResult[] {
  const parsed = parseJsonValue(value);
  const rows = isRecord(parsed) && Array.isArray(parsed.results)
    ? parsed.results
    : Array.isArray(parsed) ? parsed : [];
  return rows.flatMap((row) => {
    if (!isRecord(row) || !Number.isInteger(row.task_index)) return [];
    const tokens = isRecord(row.tokens) ? row.tokens : null;
    const inputTokens = typeof tokens?.input === 'number' ? tokens.input : 0;
    const outputTokens = typeof tokens?.output === 'number' ? tokens.output : 0;
    return [{
      taskIndex: row.task_index as number,
      status: typeof row.status === 'string' ? row.status.toLowerCase() : 'unknown',
      summary: typeof row.summary === 'string' ? row.summary.trim() : '',
      model: typeof row.model === 'string' ? row.model.trim() : '',
      durationSeconds: typeof row.duration_seconds === 'number' && Number.isFinite(row.duration_seconds) ? row.duration_seconds : null,
      error: typeof row.error === 'string' ? row.error.trim() : '',
      tokens: tokens ? inputTokens + outputTokens : null,
    }];
  });
}

function delegationStatus(status: string): 'completed' | 'failed' | 'running' | 'interrupted' | 'planned' {
  if (status === 'completed') return 'completed';
  if (status === 'failed' || status === 'error' || status === 'timeout') return 'failed';
  if (status === 'running' || status === 'streaming') return 'running';
  if (status === 'interrupted') return 'interrupted';
  return 'planned';
}

function formatDelegationDuration(seconds: number | null): string | null {
  if (seconds === null) return null;
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
}

function delegationStatusIcon(status: ReturnType<typeof delegationStatus>) {
  if (status === 'completed') return <CheckCircle2 size={13} aria-hidden />;
  if (status === 'failed' || status === 'interrupted') return <XCircle size={13} aria-hidden />;
  if (status === 'running') return <Loader2 size={13} className="chat-spin" aria-hidden />;
  return <Clock3 size={13} aria-hidden />;
}

function DelegationPanel({
  tasks,
  results,
  running,
}: {
  tasks: DelegationTask[];
  results: DelegationResult[];
  running: boolean;
}) {
  const byTask = new Map(results.map((result) => [result.taskIndex, result]));
  const indices = [...new Set([
    ...tasks.map((task) => task.taskIndex),
    ...results.map((result) => result.taskIndex),
  ])].sort((a, b) => a - b);
  if (indices.length === 0) return null;

  const completed = results.filter((result) => delegationStatus(result.status) === 'completed').length;
  const failed = results.filter((result) => delegationStatus(result.status) === 'failed').length;
  return (
    <div className="chat-tool-delegation">
      <div className="chat-tool-delegation-header">
        <span><Bot size={12} aria-hidden /> Child agents</span>
        <span>{running ? 'Dispatching' : `${completed}/${indices.length} completed`}{failed ? ` · ${failed} failed` : ''}</span>
      </div>
      <ul className="chat-tool-delegation-list">
        {indices.map((taskIndex) => {
          const task = tasks.find((candidate) => candidate.taskIndex === taskIndex);
          const result = byTask.get(taskIndex);
          const status = delegationStatus(result?.status || (running ? 'planned' : 'unknown'));
          const duration = formatDelegationDuration(result?.durationSeconds ?? null);
          return (
            <li key={taskIndex} className={`chat-tool-delegation-row is-${status}`}>
              <span className="chat-tool-delegation-icon">{delegationStatusIcon(status)}</span>
              <div className="chat-tool-delegation-copy">
                <strong>Task {taskIndex + 1}</strong>
                <span>{task?.goal || result?.summary || 'Delegated task'}</span>
                {result?.summary && task?.goal ? <small>{result.summary}</small> : null}
                {result?.error ? <small className="chat-tool-delegation-error">{result.error}</small> : null}
              </div>
              <div className="chat-tool-delegation-meta">
                <span>{status}</span>
                {result?.model ? <span title={result.model}>{compactText(result.model, 26)}</span> : null}
                {duration ? <span>{duration}</span> : null}
                {result?.tokens !== null && result?.tokens !== undefined ? <span>{result.tokens.toLocaleString()} tok</span> : null}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function familySummary(family: ToolFamily, message: ChatMessage, urls: string[]): string | null {
  const payload = [message.toolInput, message.detail, message.output, message.text].filter(Boolean).join('\n');
  if (family === 'execution') {
    const exitCode = extractExitCode(payload);
    const workdir = extractWorkdir(payload);
    return [exitCode !== null ? `exit ${exitCode}` : null, workdir].filter(Boolean).join(' · ') || 'Command or file operation';
  }
  if (family === 'research') return urls.length ? `${urls.length} source${urls.length === 1 ? '' : 's'}` : 'Search and source results';
  if (family === 'delegation') return 'Agent dispatch and result';
  if (family === 'scheduler') return 'Schedule and run state';
  if (family === 'context') return 'Retrieved context and provenance';
  if (family === 'media') return 'Generated or transcribed media';
  if (family === 'integration') return 'External action and result';
  return null;
}

function PayloadBlock({
  label,
  value,
  detail,
  open = true,
}: {
  label: string;
  value: string;
  detail?: ReactNode;
  open?: boolean;
}) {
  return (
    <details className="chat-tool-section chat-tool-payload" open={open}>
      <summary className="chat-tool-section-label"><span>{label}</span>{detail}</summary>
      <pre>{value}</pre>
    </details>
  );
}

function SourceLinks({ urls }: { urls: string[] }) {
  if (urls.length === 0) return null;
  return (
    <div className="chat-tool-sources" aria-label="Sources">
      <div className="chat-tool-section-label"><span>Sources</span><span>{urls.length}</span></div>
      <div className="chat-tool-source-list">
        {urls.slice(0, 6).map((url) => (
          <a key={url} href={url} target="_blank" rel="noreferrer" className="chat-tool-source">
            <Globe2 size={12} aria-hidden />
            <span>{compactText(url.replace(/^https?:\/\//, ''), 72)}</span>
            <ExternalLink size={11} aria-hidden />
          </a>
        ))}
      </div>
    </div>
  );
}

function FamilyMarker({ family, message, urls }: { family: ToolFamily; message: ChatMessage; urls: string[] }) {
  const summary = familySummary(family, message, urls);
  if (!summary) return null;
  return (
    <div className="chat-tool-summary">
      <span className="chat-tool-family-label">{FAMILY_META[family].label}</span>
      <span className="chat-tool-summary-text">{summary}</span>
    </div>
  );
}

export function ToolMessage({ message }: { message: ChatMessage }) {
  const family = classifyTool(message.toolName);
  const meta = FAMILY_META[family];
  const FamilyIcon = meta.icon;
  const running = message.status === 'streaming';
  const failed = message.status === 'error';
  const input = message.toolInput || message.text;
  const payload = [message.toolInput, message.detail, message.output, message.text].filter(Boolean).join('\n');
  const urls = extractUrls(payload);
  const delegationTasks = family === 'delegation' ? parseDelegationTasks(input) : [];
  const delegationResults = family === 'delegation' ? parseDelegationResults(message.output || message.detail || '') : [];
  const duration = typeof message.durationS === 'number' && Number.isFinite(message.durationS)
    ? message.durationS < 1 ? `${Math.round(message.durationS * 1000)}ms` : `${message.durationS.toFixed(message.durationS < 10 ? 1 : 0)}s`
    : null;
  const stateLabel = failed ? 'Failed' : running ? 'Running' : 'Completed';

  return (
    <div className={`chat-tool-surface chat-tool-family-${family}`}>
      <div className="chat-tool-header">
        <span className="chat-tool-avatar" aria-hidden><FamilyIcon size={15} /></span>
        <div className="chat-tool-heading">
          <strong>{message.toolName || 'Tool'}</strong>
          <span>{meta.label}</span>
        </div>
        <span className={`chat-tool-state is-${failed ? 'error' : running ? 'running' : 'complete'}`}>
          {failed ? <XCircle size={13} /> : running ? <Loader2 size={13} className="chat-spin" /> : <CheckCircle2 size={13} />}
          {stateLabel}
        </span>
      </div>

      <FamilyMarker family={family} message={message} urls={urls} />

      {family === 'execution' && extractWorkdir(payload) ? (
        <div className="chat-tool-execution-meta"><FolderOpen size={12} aria-hidden /><span>{extractWorkdir(payload)}</span></div>
      ) : null}

      {family === 'delegation' ? <DelegationPanel tasks={delegationTasks} results={delegationResults} running={running} /> : null}
      {family === 'scheduler' ? <div className="chat-tool-family-note"><Timer size={12} aria-hidden /> Schedule operation</div> : null}
      {family === 'context' ? <div className="chat-tool-family-note"><ServerCog size={12} aria-hidden /> Context provenance</div> : null}
      {family === 'media' ? <div className="chat-tool-family-note"><Music2 size={12} aria-hidden /> Preview deferred — result remains downloadable</div> : null}
      {family === 'integration' ? <div className="chat-tool-family-note"><MessageSquare size={12} aria-hidden /> External service action</div> : null}

      {input ? (
        <PayloadBlock
          label={family === 'execution' ? 'Command / input' : 'Input'}
          value={input}
          detail={message.toolId ? <span>#{message.toolId.slice(-8)}</span> : <span>request</span>}
        />
      ) : null}

      {message.detail ? (
        <div className="chat-tool-live">
          <span><Activity size={12} /> Live output</span>
          <pre>{message.detail}</pre>
        </div>
      ) : null}

      <SourceLinks urls={family === 'research' ? urls : []} />

      {message.output ? (
        <PayloadBlock
          label="Output"
          value={message.output}
          open={running || failed}
          detail={<span>{duration ? <><Clock3 size={11} /> {duration}</> : 'result'}</span>}
        />
      ) : running ? (
        <div className="chat-tool-waiting"><Loader2 size={13} className="chat-spin" /> Waiting for tool result…</div>
      ) : null}

      {!input && !message.output && !message.detail && !running ? <div className="chat-tool-waiting">No payload returned.</div> : null}
    </div>
  );
}
