import { useI18n } from '../lib/i18n';
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { ChevronDown, CircleAlert, Download, Loader2, Plus, RefreshCw, Save, Search, Trash2 } from 'lucide-react';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { Modal } from '../components/Modal';
import { Card } from '../components/ui/Card';
import { Dropdown } from '../components/ui/Dropdown';
import { ToggleSwitch } from '../components/ui/ToggleSwitch';
import { PageHeader } from '../components/PageHeader';
import { useMissionControl } from '../lib/mission-control-store';
import {
  loadMissionControlSkillsCatalog,
  type MissionControlSkillCatalogItem,
} from '../lib/hermes-api';
import {
  configureBotMcpTools,
  configureBotProfile,
  createBotProfile,
  deleteBotProfile,
  loadBotMcpTools,
  loadBotProfile,
  loadBotProfiles,
  installBotSkill,
  loadBotModelOptions,
  openBotCanonicalChat,
  type BotModelProviderOption,
  type BotProfileDetails,
  type BotProfileSummary,
} from '../lib/bot-gateway';
import { buildBotCreateInput } from '../lib/bot-create';

const EMPTY_SOUL = `You are a specialist Hermes Bot.

Describe your role, the work you own, and the boundaries you must respect.
Be concise, evidence-based, and explicit about uncertainty.`;

type ProfileTab = 'overview' | 'tools' | 'mcp' | 'skills';

type BotDraft = {
  name: string;
  description: string;
  soul: string;
  model: string;
  provider: string;
  enabledToolsets: string[];
  enabledMcpServers: string[];
  mcpToolEnabled: Record<string, boolean>;
  disabledSkills: string[];
  noSkills: boolean;
  botRoster: boolean;
  cloneFrom: string | null;
};

function emptyDraft(): BotDraft {
  return {
    name: '',
    description: '',
    soul: EMPTY_SOUL,
    model: '',
    provider: '',
    enabledToolsets: [],
    enabledMcpServers: [],
    mcpToolEnabled: {},
    disabledSkills: [],
    noSkills: true,
    botRoster: true,
    cloneFrom: null,
  };
}

function draftFromDetails(details: BotProfileDetails, botRoster: boolean): BotDraft {
  return {
    name: details.name,
    description: details.description,
    soul: details.soul,
    model: details.model.default,
    provider: details.model.provider,
    enabledToolsets: details.toolsets.filter((toolset) => toolset.enabled).map((toolset) => toolset.name),
    enabledMcpServers: details.mcp_servers.filter((server) => server.enabled).map((server) => server.name),
    mcpToolEnabled: {},
    disabledSkills: details.skills.filter((skill) => !skill.enabled).map((skill) => skill.name),
    noSkills: details.skills.length === 0,
    botRoster,
    cloneFrom: null,
  };
}

function mcpToolIsEnabled(server: BotProfileDetails['mcp_servers'][number], toolName: string): boolean {
  const filters = server.tools;
  if (filters?.include && filters.include.length > 0) return filters.include.includes(toolName);
  if (filters?.exclude && filters.exclude.length > 0) return !filters.exclude.includes(toolName);
  return true;
}

function mcpToolKey(serverName: string, toolName: string): string {
  return `${serverName}:${toolName}`;
}

function mcpToolChanges(details: BotProfileDetails, draft: BotDraft): { enable: string[]; disable: string[] } {
  const enable: string[] = [];
  const disable: string[] = [];
  for (const [key, desired] of Object.entries(draft.mcpToolEnabled)) {
    const separator = key.indexOf(':');
    if (separator <= 0) continue;
    const serverName = key.slice(0, separator);
    const toolName = key.slice(separator + 1);
    const server = details.mcp_servers.find((item) => item.name === serverName);
    if (!server || desired === mcpToolIsEnabled(server, toolName)) continue;
    (desired ? enable : disable).push(key);
  }
  return { enable, disable };
}

function profileInitials(name: string): string {
  return name.slice(0, 2).toUpperCase();
}

function BotRoster({
  profiles,
  selectedName,
  loading,
  onSelect,
  emptyMessage,
}: {
  profiles: BotProfileSummary[];
  selectedName: string | null;
  loading: boolean;
  onSelect: (name: string) => void;
  emptyMessage?: string;
}) {
  const { t } = useI18n();
  return (
    <Card padding="none" className="!border-0 overflow-hidden">
      <div className="flex items-center justify-between px-4 py-4">
        <div>
          <p className="eyebrow">{t('bots.roster')}</p>
          <p className="mt-1 text-xs text-text-muted">{t('bots.profilesCount', { count: profiles.length })}</p>
        </div>
      </div>
      <div className="divide-y divide-white/5">
        {loading && profiles.length === 0 ? (
          <div className="flex items-center gap-2 px-4 py-8 text-sm text-text-muted">
            <Loader2 size={16} className="animate-spin" /> {t('bots.loadingRoster')}
          </div>
        ) : profiles.length === 0 ? (
          <div className="px-4 py-8 text-sm text-text-muted">{emptyMessage || t('bots.noProfiles')}</div>
        ) : profiles.map((profile) => {
          const selected = profile.name === selectedName;
          return (
            <button
              key={profile.name}
              type="button"
              onClick={() => onSelect(profile.name)}
              className={`flex w-full items-center gap-3 px-4 py-3 text-left transition-colors ${selected ? 'bg-accent/10' : 'hover:bg-surface-sunken/40'}`}
            >
              <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl text-xs font-semibold ${selected ? 'bg-accent text-white' : 'bg-surface-sunken text-text-muted'}`}>
                {profileInitials(profile.display_name || profile.name)}
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-2">
                  <span className="truncate text-sm font-semibold text-text">{profile.display_name || profile.name}</span>
                  {profile.is_default ? <Badge variant="default">default</Badge> : null}
                </span>
                <span className="mt-1 block truncate text-xs text-text-muted">
                  {profile.description || t('bots.noRole')}
                </span>
                <span className="mt-1 block truncate text-[11px] text-text-subtle">
                  {profile.provider || t('bots.inheritProvider')} · {profile.model || t('bots.inheritModel')} · {profile.canonical_session ? t('bots.chatReady') : t('bots.chatNotCreated')}
                </span>
              </span>
              <span className={`h-2 w-2 shrink-0 rounded-full ${profile.canonical_session ? 'bg-positive' : 'bg-text-subtle/40'}`} aria-hidden />
            </button>
          );
        })}
      </div>
    </Card>
  );
}

function ProfileEditor({
  mode,
  draft,
  details,
  createToolsets,
  createMcpServers,
  createToolsetsLoading,
  startingProfiles,
  modelOptions,
  busy,
  error,
  onChange,
  onSubmit,
  onCancelCreate,
  onClose,
  accessToken,
  onInstallSkill,
  onDelete,
}: {
  mode: 'edit' | 'create';
  draft: BotDraft;
  details: BotProfileDetails | null;
  createToolsets: BotProfileDetails['toolsets'];
  createMcpServers: BotProfileDetails['mcp_servers'];
  createToolsetsLoading: boolean;
  startingProfiles: BotProfileSummary[];
  modelOptions: BotModelProviderOption[];
  busy: boolean;
  error: string | null;
  onChange: (next: Partial<BotDraft>) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onCancelCreate: () => void;
  onClose: () => void;
  accessToken?: string;
  onInstallSkill: (profile: string, identifier: string) => Promise<void>;
  onDelete: () => Promise<void>;
}) {
  const { t } = useI18n();
  const [activeTab, setActiveTab] = useState<ProfileTab>('overview');
  const [skillCatalog, setSkillCatalog] = useState<MissionControlSkillCatalogItem[]>([]);
  const skillCatalogRequestedRef = useRef(false);
  const soulRef = useRef<HTMLTextAreaElement>(null);
  const [skillCatalogLoading, setSkillCatalogLoading] = useState(false);
  const [skillCatalogError, setSkillCatalogError] = useState<string | null>(null);
  const [skillQuery, setSkillQuery] = useState('');
  const [installingSkill, setInstallingSkill] = useState<string | null>(null);
  const [mcpTools, setMcpTools] = useState<Record<string, Array<{ name: string; description?: string }>>>({});
  const [mcpToolsLoading, setMcpToolsLoading] = useState(false);
  const [mcpToolsError, setMcpToolsError] = useState<Record<string, string>>({});
  const [expandedMcpTools, setExpandedMcpTools] = useState<Record<string, boolean>>({});
  const mcpToolsRequestedRef = useRef<Set<string>>(new Set());
  const toolsets = details?.toolsets ?? createToolsets;
  const toolsetsPinned = details?.toolsets_pinned ?? (mode === 'create' && draft.enabledToolsets.length > 0);
  const skills = details?.skills ?? [];
  const mcpServers = details?.mcp_servers ?? createMcpServers;
  const providerOptions = useMemo(() => {
    const options = [
      { value: '', label: t('bots.inheritProvider') },
      ...modelOptions
        .filter((provider) => provider.models.length > 0 || provider.slug === draft.provider)
        .map((provider) => ({ value: provider.slug, label: `${provider.name} (${provider.slug})` })),
    ];
    if (draft.provider && !options.some((option) => option.value === draft.provider)) {
      options.push({ value: draft.provider, label: draft.provider });
    }
    return options;
  }, [draft.provider, modelOptions, t]);
  const providerModels = modelOptions.find((provider) => provider.slug === draft.provider)?.models ?? [];
  const modelSelectOptions = useMemo(() => {
    const options = [{ value: '', label: t('bots.inheritModel') }, ...providerModels.map((model) => ({ value: model, label: model }))];
    if (draft.model && !options.some((option) => option.value === draft.model)) {
      options.push({ value: draft.model, label: draft.model });
    }
    return options;
  }, [draft.model, providerModels, t]);
  const startingProfileOptions = useMemo(() => [
    { value: '', label: t('bots.freshProfile') },
    ...startingProfiles.map((profile) => ({
      value: profile.name,
      label: `${profile.display_name || profile.name}${profile.is_default ? ` · ${t('bots.defaultProfile')}` : ''}`,
    })),
  ], [startingProfiles, t]);
  const toggleValue = (values: string[], value: string) => values.includes(value)
    ? values.filter((item) => item !== value)
    : [...values, value];

  useEffect(() => {
    if (activeTab !== 'skills' || skillCatalogRequestedRef.current) return;
    skillCatalogRequestedRef.current = true;
    let cancelled = false;
    setSkillCatalogLoading(true);
    setSkillCatalogError(null);
    let timeoutId: number | undefined;
    const catalogRequest = loadMissionControlSkillsCatalog(accessToken, { limit: 5000 });
    const timeoutRequest = new Promise<never>((_, reject) => {
      timeoutId = window.setTimeout(() => reject(new Error('skill_catalog_timeout')), 15000);
    });
    Promise.race([catalogRequest, timeoutRequest]).then((result) => {
      if (cancelled) return;
      setSkillCatalog(result.skills);
      if (!result.available) setSkillCatalogError(result.hint ?? t('bots.skillsCatalogUnavailable'));
    }).catch((cause) => {
      if (!cancelled) setSkillCatalogError(cause instanceof Error && cause.message === 'skill_catalog_timeout'
        ? t('bots.skillsCatalogTimeout')
        : t('bots.skillsCatalogUnavailable'));
    }).finally(() => {
      if (timeoutId !== undefined) window.clearTimeout(timeoutId);
      if (!cancelled) setSkillCatalogLoading(false);
    });
    return () => {
      cancelled = true;
      if (timeoutId !== undefined) window.clearTimeout(timeoutId);
    };
  // Request ownership is intentionally guarded by a ref: loading state must not cancel its own request.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, accessToken]);

  useEffect(() => {
    mcpToolsRequestedRef.current = new Set();
    setMcpTools({});
    setMcpToolsError({});
    setExpandedMcpTools({});
  }, [details?.name, mode]);

  useEffect(() => {
    if (activeTab !== 'mcp' || mode === 'create') return;
    const pending = mcpServers.filter((server) => draft.enabledMcpServers.includes(server.name) && !mcpToolsRequestedRef.current.has(server.name));
    if (pending.length === 0) return;
    pending.forEach((server) => mcpToolsRequestedRef.current.add(server.name));
    let cancelled = false;
    setMcpToolsLoading(true);
    setMcpToolsError({});
    Promise.all(pending.map(async (server) => ({
      name: server.name,
      result: await loadBotMcpTools(server.name, server.configured ? draft.name : undefined, accessToken),
    }))).then((results) => {
      if (cancelled) return;
      setMcpTools((current) => Object.fromEntries([
        ...Object.entries(current),
        ...results.filter(({ result }) => result.ok).map(({ name, result }) => [name, result.tools]),
      ]));
      const errors = Object.fromEntries(
        results.filter(({ result }) => !result.ok && result.error).map(({ name, result }) => [name, result.error as string]),
      );
      setMcpToolsError(errors);
    }).catch((cause) => {
      if (!cancelled) setMcpToolsError({ _gateway: cause instanceof Error ? cause.message : t('bots.mcpToolsUnavailable') });
    }).finally(() => {
      if (!cancelled) setMcpToolsLoading(false);
    });
    return () => { cancelled = true; };
  }, [accessToken, activeTab, draft.name, mcpServers, mode, t]);

  useEffect(() => {
    const textarea = soulRef.current;
    if (!textarea || activeTab !== 'overview') return;
    textarea.style.height = '0px';
    textarea.style.height = `${Math.max(textarea.scrollHeight, 256)}px`;
  }, [activeTab, draft.soul]);

  const installedSkillNames = useMemo(() => new Set(skills.map((skill) => skill.name.toLowerCase())), [skills]);
  const visibleSkillCatalog = useMemo(() => {
    const query = skillQuery.trim().toLowerCase();
    const matching = skillCatalog.filter((skill) => {
      if (!query) return true;
      return [skill.name, skill.description, skill.source, skill.identifier, ...skill.tags]
        .join(' ')
        .toLowerCase()
        .includes(query);
    });
    if (!query) return matching.slice(0, 120);
    const relevance = (skill: MissionControlSkillCatalogItem): number => {
      const name = skill.name.toLowerCase();
      const identifier = skill.identifier.toLowerCase();
      if (name === query) return 0;
      if (name.startsWith(query)) return 1;
      if (identifier === query || identifier.endsWith(`/${query}`)) return 2;
      if (identifier.includes(query)) return 3;
      return 4;
    };
    return matching.sort((left, right) => relevance(left) - relevance(right) || left.name.localeCompare(right.name)).slice(0, 120);
  }, [skillCatalog, skillQuery]);

  const handleInstallSkill = async (skill: MissionControlSkillCatalogItem) => {
    if (mode === 'create' || installingSkill) return;
    if (!window.confirm(t('bots.skillInstallConfirm', { name: skill.name }))) return;
    setInstallingSkill(skill.identifier);
    setSkillCatalogError(null);
    try {
      await onInstallSkill(draft.name, skill.identifier);
    } catch (cause) {
      setSkillCatalogError(cause instanceof Error ? cause.message : t('bots.skillInstallFailed'));
    } finally {
      setInstallingSkill(null);
    }
  };

  return (
    <Modal
      open
      title={mode === 'create' ? t('bots.createTitle') : draft.name}
      subtitle={mode === 'create' ? t('bots.createDescription') : draft.description || t('bots.profileDescription')}
      onClose={onClose}
      borderless
      fixedHeight
      className="bot-detail-modal"
      footer={(
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            {mode === 'edit' ? (
              <Button type="button" variant="danger" icon={<Trash2 size={15} />} onClick={() => void onDelete()} disabled={busy}>
                {t('bots.delete')}
              </Button>
            ) : null}
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            {mode === 'create' ? <Button type="button" variant="ghost" onClick={onCancelCreate} disabled={busy}>{t('bots.cancel')}</Button> : null}
            <Button type="submit" form="bot-profile-form" variant="primary" icon={busy ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />} disabled={busy}>
              {busy ? t('bots.saving') : mode === 'create' ? t('bots.create') : t('bots.save')}
            </Button>
          </div>
        </div>
      )}
    >
      <form id="bot-profile-form" className="space-y-5" onSubmit={onSubmit}>
        <div className="flex flex-wrap gap-1 rounded-xl bg-surface-sunken/35 p-1">
          {([['overview', t('bots.overview')], ['tools', t('bots.toolsets')], ['mcp', t('bots.mcp')], ['skills', t('bots.skills')]] as const).map(([tab, label]) => (
            <button
              key={tab}
              type="button"
              onClick={() => setActiveTab(tab)}
              className={`rounded-lg px-3 py-2 text-xs font-medium transition-colors ${activeTab === tab ? 'bg-surface-raised text-text shadow-sm' : 'text-text-muted hover:bg-surface-raised/60 hover:text-text'}`}
            >
              {label}
            </button>
          ))}
        </div>

        {activeTab === 'overview' ? (
          <div className="space-y-5">
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="block text-xs font-medium text-text-muted sm:col-span-2">
            {t('bots.name')}
            <input
              className="mt-1.5 w-full rounded-lg bg-surface-sunken/40 shadow-inner px-3 py-2 text-sm text-text outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20 disabled:opacity-60"
              value={draft.name}
              onChange={(event) => onChange({ name: event.target.value.toLowerCase() })}
              placeholder={t('bots.namePlaceholder')}
              disabled={mode === 'edit' || busy}
              pattern="[a-z0-9][a-z0-9_-]{0,63}"
              required
            />
          </label>
          <label className="block text-xs font-medium text-text-muted sm:col-span-2">
            {t('bots.descriptionLabel')}
            <textarea
              className="mt-1.5 min-h-24 w-full resize-y rounded-lg bg-surface-sunken/40 shadow-inner px-3 py-2 text-sm leading-relaxed text-text outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
              value={draft.description}
              onChange={(event) => onChange({ description: event.target.value })}
              placeholder={t('bots.rolePlaceholder')}
              disabled={busy}
              rows={3}
            />
          </label>
        </div>

        {mode === 'create' ? (
          <label className="block text-xs font-medium text-text-muted">
            {t('bots.startingProfile')}
            <div className="mt-1.5">
              <Dropdown
                value={draft.cloneFrom ?? ''}
                options={startingProfileOptions}
                onChange={(cloneFrom) => onChange({
                  cloneFrom: cloneFrom || null,
                  noSkills: cloneFrom ? false : draft.noSkills,
                })}
                ariaLabel={t('bots.startingProfile')}
                disabled={busy}
              />
            </div>
            <span className="mt-1.5 block text-[11px] font-normal text-text-subtle">{t('bots.startingProfileHelp')}</span>
          </label>
        ) : null}

        <div className="grid gap-4 sm:grid-cols-2">
          <label className="block text-xs font-medium text-text-muted">
            {t('bots.provider')}
            <div className="mt-1.5">
              <Dropdown
                value={draft.provider}
                options={providerOptions}
                onChange={(provider) => {
                  const nextModels = modelOptions.find((item) => item.slug === provider)?.models ?? [];
                  onChange({ provider, model: nextModels.includes(draft.model) ? draft.model : '' });
                }}
                ariaLabel={t('bots.provider')}
                disabled={busy}
              />
            </div>
          </label>
          <label className="block text-xs font-medium text-text-muted">
            {t('bots.model')}
            <div className="mt-1.5">
              <Dropdown
                value={draft.model}
                options={modelSelectOptions}
                onChange={(model) => onChange({ model })}
                ariaLabel={t('bots.model')}
                disabled={busy || !draft.provider}
              />
            </div>
          </label>
        </div>

        <label className="block text-xs font-medium text-text-muted">
          {t('bots.soul')}
          <textarea
            ref={soulRef}
            className="mt-1.5 min-h-64 w-full resize-none overflow-hidden rounded-lg bg-surface-sunken/40 shadow-inner px-3 py-2 font-mono text-xs leading-relaxed text-text outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
            value={draft.soul}
            onChange={(event) => onChange({ soul: event.target.value })}
            disabled={busy}
          />
        </label>

        {mode === 'create' ? (
          <label className="flex items-start gap-3 rounded-lg bg-surface-sunken/25 p-3 text-xs text-text-muted">
            <input
              type="checkbox"
              className="mt-0.5 accent-[var(--accent)]"
              checked={draft.noSkills}
              onChange={(event) => onChange({ noSkills: event.target.checked })}
              disabled={busy || draft.cloneFrom !== null}
            />
            <span>
              <strong className="text-text">{t('bots.createEmpty')}</strong>
              <span className="mt-1 block">{t('bots.createEmptyHelp')}</span>
            </span>
          </label>
        ) : null}

          </div>
        ) : null}

        {activeTab === 'tools' ? (
          <div className="space-y-6">
            <section>
              <div className="mb-3 flex items-center justify-between gap-3">
                <div>
                  <p className="text-xs font-semibold text-text">{t('bots.toolsets')}</p>
                  <p className="mt-1 text-[11px] text-text-subtle">{t('bots.toolsetsHelp')}</p>
                </div>
                <Badge variant={toolsetsPinned ? 'accent' : 'default'}>{toolsetsPinned ? t('bots.pinned') : t('bots.inherit')}</Badge>
              </div>
              {createToolsetsLoading && mode === 'create' ? (
                <div className="flex items-center gap-2 rounded-lg bg-surface-sunken/20 px-3 py-6 text-xs text-text-muted"><Loader2 size={14} className="animate-spin" /> {t('bots.loadingToolsets')}</div>
              ) : toolsets.length > 0 ? (
                <div className="grid gap-2 sm:grid-cols-2">
                  {toolsets.map((toolset) => {
                    const enabled = draft.enabledToolsets.includes(toolset.name);
                    return (
                      <label key={toolset.name} className={`flex cursor-pointer items-start gap-2 rounded-lg p-3 transition-colors ${enabled ? 'bg-accent/10' : 'bg-surface-sunken/20 hover:bg-surface-sunken/40'}`}>
                        <input
                          type="checkbox"
                          className="mt-0.5 accent-[var(--accent)]"
                          checked={enabled}
                          onChange={() => onChange({ enabledToolsets: toggleValue(draft.enabledToolsets, toolset.name) })}
                          disabled={busy}
                        />
                        <span className="min-w-0">
                          <span className="block text-xs font-medium text-text">{toolset.label || toolset.name}</span>
                          <span className="mt-1 block text-[11px] leading-relaxed text-text-subtle">{toolset.description || `${toolset.tool_count ?? 0} tools`}</span>
                        </span>
                      </label>
                    );
                  })}
                </div>
              ) : (
                <p className="rounded-lg bg-surface-sunken/20 px-3 py-6 text-xs text-text-muted">{t('bots.noToolsets')}</p>
              )}
            </section>
          </div>
        ) : null}

        {activeTab === 'mcp' ? (
          <div className="space-y-6">
            <section>
              <div className="mb-3">
                <p className="text-xs font-semibold text-text">{t('bots.mcp')}</p>
                <p className="mt-1 text-[11px] text-text-subtle">{t('bots.mcpHelp')}</p>
              </div>
              {mcpToolsError._gateway ? <p className="mb-3 rounded-lg bg-warning/10 px-3 py-2 text-xs text-warning">{mcpToolsError._gateway}</p> : null}
              {mcpServers.length > 0 ? (
                <div className="space-y-3">
                  {mcpServers.map((server) => {
                    const serverEnabled = draft.enabledMcpServers.includes(server.name);
                    const discovered = mcpTools[server.name] ?? [];
                    const toolsExpanded = expandedMcpTools[server.name] === true;
                    return (
                      <div key={server.name} className="rounded-xl bg-surface-sunken/20 p-3">
                        <div className="flex items-center justify-between gap-3">
                          <div className="min-w-0">
                            <p className="truncate text-xs font-semibold text-text">{server.name}</p>
                            <p className="mt-1 text-[11px] text-text-subtle">{server.transport || 'MCP'} · {server.configured ? (serverEnabled ? t('bots.enabled') : t('bots.disabled')) : t('bots.mcpAvailable')}</p>
                          </div>
                          <ToggleSwitch
                            id={`mcp-server-${server.name}`}
                            checked={serverEnabled}
                            onChange={() => onChange({ enabledMcpServers: toggleValue(draft.enabledMcpServers, server.name) })}
                            disabled={busy}
                            label={serverEnabled ? t('bots.enabled') : t('bots.disabled')}
                          />
                        </div>
                        {serverEnabled && mcpToolsError[server.name] ? <p className="mt-3 rounded-lg bg-warning/10 px-2.5 py-2 text-[11px] leading-relaxed text-warning">{mcpToolsError[server.name]}</p> : null}
                        {serverEnabled ? (
                          <div className="mt-3 border-t border-white/5 pt-3">
                            {mcpToolsLoading && discovered.length === 0 ? (
                              <div className="flex items-center gap-2 text-[11px] text-text-muted"><Loader2 size={13} className="animate-spin" /> {t('bots.mcpToolsLoading')}</div>
                            ) : discovered.length > 0 ? (
                              <div className="space-y-2">
                                <button
                                  type="button"
                                  className="flex w-full items-center justify-between gap-3 rounded-lg px-2 py-2 text-left text-[11px] font-medium uppercase tracking-wide text-text-subtle transition-colors hover:bg-surface-raised/40 hover:text-text"
                                  aria-expanded={toolsExpanded}
                                  onClick={() => setExpandedMcpTools((current) => ({ ...current, [server.name]: !toolsExpanded }))}
                                >
                                  <span>{t('bots.individualTools')} <span className="normal-case text-text-muted">({discovered.length})</span></span>
                                  <ChevronDown size={14} className={`shrink-0 transition-transform ${toolsExpanded ? 'rotate-180' : ''}`} />
                                </button>
                                {toolsExpanded ? (
                                  <div className="space-y-1">
                                    {discovered.map((tool) => {
                                      const key = mcpToolKey(server.name, tool.name);
                                      const enabled = key in draft.mcpToolEnabled
                                        ? draft.mcpToolEnabled[key]
                                        : mcpToolIsEnabled(server, tool.name);
                                      return (
                                        <label key={tool.name} className={`flex cursor-pointer items-start gap-2 rounded-lg px-2 py-2 ${enabled ? 'bg-accent/5' : 'hover:bg-surface-raised/40'}`}>
                                          <input
                                            type="checkbox"
                                            className="mt-0.5 accent-[var(--accent)]"
                                            checked={enabled}
                                            onChange={() => onChange({ mcpToolEnabled: { ...draft.mcpToolEnabled, [key]: !enabled } })}
                                            disabled={busy}
                                          />
                                          <span className="min-w-0">
                                            <span className="block break-all font-mono text-[11px] text-text">{tool.name}</span>
                                            {tool.description ? <span className="mt-0.5 block line-clamp-2 text-[11px] leading-relaxed text-text-subtle">{tool.description}</span> : null}
                                          </span>
                                        </label>
                                      );
                                    })}
                                  </div>
                                ) : null}
                              </div>
                            ) : (
                              mcpToolsError[server.name] ? null : <p className="text-[11px] text-text-muted">{t('bots.mcpToolsUnavailable')}</p>
                            )}
                          </div>
                        ) : null}
                      </div>
                    );
                  })}
                </div>
              ) : (
                <p className="rounded-lg bg-surface-sunken/20 px-3 py-6 text-xs text-text-muted">{t('bots.noMcpServers')}</p>
              )}
            </section>
          </div>
        ) : null}

        {activeTab === 'skills' ? (
          <div className="space-y-6">
            <section>
              <div className="mb-3 flex items-center justify-between gap-3">
                <div>
                  <p className="text-xs font-semibold text-text">{t('bots.skillsInstalled')}</p>
                  <p className="mt-1 text-[11px] text-text-subtle">{t('bots.skillsInstalledHelp')}</p>
                </div>
                <Badge variant="default">{skills.length}</Badge>
              </div>
              {skills.length > 0 ? (
                <div className="divide-y divide-white/5 rounded-lg bg-surface-sunken/20">
                  {skills.map((skill) => {
                    const enabled = !draft.disabledSkills.includes(skill.name);
                    return (
                      <label key={skill.name} className="flex items-center justify-between gap-3 px-3 py-2.5 text-xs">
                        <span className="min-w-0 truncate font-mono text-text">{skill.name}</span>
                        <span className="flex shrink-0 items-center gap-2 text-[11px] text-text-subtle">
                          {enabled ? t('bots.enabled') : t('bots.disabled')}
                          <input
                            type="checkbox"
                            className="accent-[var(--accent)]"
                            checked={enabled}
                            onChange={() => onChange({ disabledSkills: toggleValue(draft.disabledSkills, skill.name) })}
                            disabled={busy}
                          />
                        </span>
                      </label>
                    );
                  })}
                </div>
              ) : (
                <p className="rounded-lg bg-surface-sunken/20 px-3 py-4 text-xs text-text-muted">{t('bots.noInstalledSkills')}</p>
              )}
            </section>

            <section className="border-t border-white/5 pt-5">
              <div className="mb-3 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
                <div>
                  <p className="text-xs font-semibold text-text">{t('bots.skillsCatalog')}</p>
                  <p className="mt-1 text-[11px] text-text-subtle">{t('bots.skillsCatalogHelp')}</p>
                </div>
                <label className="relative block sm:w-64">
                  <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-subtle" />
                  <input
                    value={skillQuery}
                    onChange={(event) => setSkillQuery(event.target.value)}
                    placeholder={t('bots.searchSkills')}
                    aria-label={t('bots.searchSkills')}
                    className="h-9 w-full rounded-lg bg-surface-sunken/40 pl-9 pr-3 text-xs text-text outline-none focus:ring-2 focus:ring-accent/20"
                  />
                </label>
              </div>
              {skillCatalogError ? <p className="mb-3 rounded-lg bg-warning/10 px-3 py-2 text-xs text-warning">{skillCatalogError}</p> : null}
              {skillCatalogLoading ? (
                <div className="flex items-center gap-2 rounded-lg bg-surface-sunken/20 px-3 py-6 text-xs text-text-muted"><Loader2 size={14} className="animate-spin" /> {t('bots.skillsCatalogLoading')}</div>
              ) : visibleSkillCatalog.length > 0 ? (
                <div className="divide-y divide-white/5 rounded-lg bg-surface-sunken/20">
                  {visibleSkillCatalog.map((skill) => {
                    const installed = installedSkillNames.has(skill.name.toLowerCase());
                    return (
                      <div key={`${skill.source}:${skill.identifier}`} className="flex items-center gap-3 px-3 py-2.5">
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-xs font-medium text-text">{skill.name}</p>
                          <p className="mt-0.5 line-clamp-2 text-[11px] leading-relaxed text-text-subtle">{skill.description || t('bots.noSkillDescription')}</p>
                        </div>
                        {installed ? (
                          <Badge variant="positive">{t('bots.installed')}</Badge>
                        ) : (
                          <Button
                            type="button"
                            size="sm"
                            variant="secondary"
                            icon={<Download size={13} />}
                            loading={installingSkill === skill.identifier}
                            disabled={mode === 'create' || installingSkill !== null}
                            onClick={() => void handleInstallSkill(skill)}
                            title={mode === 'create' ? t('bots.saveBeforeInstall') : t('bots.install')}
                          >
                            {t('bots.install')}
                          </Button>
                        )}
                      </div>
                    );
                  })}
                </div>
              ) : (
                <p className="rounded-lg bg-surface-sunken/20 px-3 py-6 text-xs text-text-muted">{t('bots.noSkillMatches')}</p>
              )}
            </section>
          </div>
        ) : null}

        {error ? (
          <div className="flex items-start gap-2 rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs text-warning">
            <CircleAlert size={15} className="mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        ) : null}

      </form>
    </Modal>
  );
}

export function BotsRoute() {
  const { t } = useI18n();
  const { storedToken } = useMissionControl();
  const [profiles, setProfiles] = useState<BotProfileSummary[]>([]);
  const [modelOptions, setModelOptions] = useState<BotModelProviderOption[]>([]);
  const [createToolsets, setCreateToolsets] = useState<BotProfileDetails['toolsets']>([]);
  const [createMcpServers, setCreateMcpServers] = useState<BotProfileDetails['mcp_servers']>([]);
  const [createToolsetsLoading, setCreateToolsetsLoading] = useState(false);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [details, setDetails] = useState<BotProfileDetails | null>(null);
  const [draft, setDraft] = useState<BotDraft>(() => emptyDraft());
  const [mode, setMode] = useState<'edit' | 'create'>('edit');
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refreshRoster = useCallback(async (preferredName?: string | null) => {
    setLoading(true);
    setError(null);
    try {
      const result = await loadBotProfiles(storedToken || undefined);
      setProfiles(result.profiles);
      const botProfiles = result.profiles.filter((profile) => profile.is_bot === true);
      const preferred = preferredName && botProfiles.some((profile) => profile.name === preferredName)
        ? preferredName
        : botProfiles[0]?.name || null;
      setSelectedName(preferred);
      if (!preferred) {
        setMode('create');
        setDraft(emptyDraft());
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not load the Bot roster.');
    } finally {
      setLoading(false);
    }
  }, [storedToken]);

  useEffect(() => { void refreshRoster(); }, [refreshRoster]);

  const refreshDetails = useCallback(async (name: string) => {
    setDetailLoading(true);
    setError(null);
    try {
      const result = await loadBotProfile(name, storedToken || undefined);
      setDetails(result);
      setDraft(draftFromDetails(result, profiles.find((profile) => profile.name === name)?.is_bot === true));
    } catch (cause) {
      setDetails(null);
      setError(cause instanceof Error ? cause.message : 'Could not load this Bot profile.');
    } finally {
      setDetailLoading(false);
    }
  }, [profiles, storedToken]);

  const handleInstallBotSkill = useCallback(async (profile: string, identifier: string) => {
    await installBotSkill(profile, identifier, storedToken || undefined);
    await refreshDetails(profile);
  }, [refreshDetails, storedToken]);

  const loadCreateToolsets = useCallback(async () => {
    setCreateToolsetsLoading(true);
    try {
      const template = await loadBotProfile('default', storedToken || undefined);
      setCreateToolsets(template.toolsets);
      setCreateMcpServers(template.mcp_servers);
    } catch (cause) {
      setCreateToolsets([]);
      setCreateMcpServers([]);
      setError(cause instanceof Error ? cause.message : 'Could not load the toolset catalog.');
    } finally {
      setCreateToolsetsLoading(false);
    }
  }, [storedToken]);

  useEffect(() => {
    if (!detailOpen || modelOptions.length > 0) return;
    let cancelled = false;
    loadBotModelOptions(storedToken || undefined).then((options) => {
      if (!cancelled) setModelOptions(options);
    }).catch(() => {
      if (!cancelled) setModelOptions([]);
    });
    return () => { cancelled = true; };
  }, [detailOpen, modelOptions.length, storedToken]);

  useEffect(() => {
    if (detailOpen && mode === 'edit' && selectedName) void refreshDetails(selectedName);
  }, [detailOpen, mode, refreshDetails, selectedName]);

  const visibleProfiles = useMemo(
    () => profiles.filter((profile) => profile.is_bot === true),
    [profiles],
  );

  const selectedSummary = useMemo(() => profiles.find((profile) => profile.name === selectedName) ?? null, [profiles, selectedName]);

  const startCreate = () => {
    setMode('create');
    setDetailOpen(true);
    setSelectedName(null);
    setDetails(null);
    setDraft(emptyDraft());
    setError(null);
    void loadCreateToolsets();
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === 'create') {
        const createInput = buildBotCreateInput(draft);
        await createBotProfile({
          ...createInput,
          shareAuth: true,
        }, storedToken || undefined);
        await configureBotProfile({
          name: draft.name,
          description: draft.description,
          soul: draft.soul,
          model: draft.model,
          provider: draft.provider,
          enabledToolsets: draft.enabledToolsets,
          enabledMcpServers: draft.enabledMcpServers,
          botRoster: draft.botRoster,
        }, storedToken || undefined);
        setMode('edit');
        setSelectedName(draft.name);
        await refreshRoster(draft.name);
      } else {
        await configureBotProfile({
          name: draft.name,
          description: draft.description,
          soul: draft.soul,
          model: draft.model,
          provider: draft.provider,
          enabledToolsets: details ? draft.enabledToolsets : undefined,
          enabledMcpServers: details ? draft.enabledMcpServers : undefined,
          disabledSkills: details ? draft.disabledSkills : undefined,
          botRoster: draft.botRoster,
        }, storedToken || undefined);
        if (details) {
          const changes = mcpToolChanges(details, draft);
          if (changes.enable.length > 0 || changes.disable.length > 0) {
            let sessionId = selectedSummary?.canonical_session?.id || '';
            if (!sessionId) {
              const chat = await openBotCanonicalChat(draft.name, undefined, storedToken || undefined);
              sessionId = chat.registryId;
            }
            await configureBotMcpTools(sessionId, 'disable', changes.disable, storedToken || undefined);
            await configureBotMcpTools(sessionId, 'enable', changes.enable, storedToken || undefined);
          }
        }
        await refreshRoster(draft.name);
        await refreshDetails(draft.name);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not save the Bot profile.');
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    const target = selectedSummary;
    if (!target || target.is_default || target.name === 'default') return;
    const label = target.display_name || target.name;
    if (!window.confirm(t('bots.deleteConfirm', { name: label }))) return;
    setBusy(true);
    setError(null);
    try {
      await deleteBotProfile(target.name);
      setDetailOpen(false);
      setDetails(null);
      setSelectedName(null);
      await refreshRoster();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t('bots.deleteFailed'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="route-page-scroll flex h-full min-w-0 flex-col gap-5 overflow-y-auto sm:gap-6">
      <PageHeader
        eyebrow={t('bots.eyebrow')}
        title={t('bots.title')}
        description={t('bots.description')}
        meta={selectedSummary ? `${t('bots.profilesCount', { count: profiles.length })} · ${selectedSummary.name}` : t('bots.profilesCount', { count: profiles.length })}
        actions={(
          <div className="bots-page-actions flex w-full min-w-0 flex-wrap items-center gap-2 sm:w-auto sm:justify-end">
            <Button size="sm" className="min-w-0 flex-1 sm:flex-none" variant="secondary" icon={<RefreshCw size={14} className={loading ? 'animate-spin' : ''} />} onClick={() => void refreshRoster(selectedName)} disabled={loading}>
              {t('bots.refresh')}
            </Button>
            <Button size="sm" className="min-w-0 flex-1 sm:flex-none" variant="primary" icon={<Plus size={14} />} onClick={startCreate}>
              {t('bots.new')}
            </Button>
          </div>
        )}
      />

      <div className="w-full px-2 pb-6 sm:px-4">
        <BotRoster
          profiles={visibleProfiles}
          selectedName={selectedName}
          loading={loading}
          onSelect={(name) => { setMode('edit'); setDetailOpen(true); setSelectedName(name); setError(null); }}
          emptyMessage={t('bots.noMarkedBots')}
        />

      {detailOpen ? (
        mode === 'edit' && detailLoading ? (
          <Modal open title={draft.name || t('bots.profileEyebrow')} subtitle={t('bots.loadingProfile')} onClose={() => setDetailOpen(false)}>
            <div className="flex min-h-64 items-center justify-center text-sm text-text-muted">
              <span className="inline-flex items-center gap-2"><Loader2 size={16} className="animate-spin" /> {t('bots.loadingProfile')}</span>
            </div>
          </Modal>
        ) : (
          <ProfileEditor
            mode={mode}
            draft={draft}
            details={details}
            createToolsets={createToolsets}
            createMcpServers={createMcpServers}
            createToolsetsLoading={createToolsetsLoading}
            startingProfiles={profiles}
            modelOptions={modelOptions}
            accessToken={storedToken || undefined}
            busy={busy}
            error={error}
            onChange={(next) => setDraft((current) => ({ ...current, ...next }))}
            onSubmit={handleSubmit}
            onCancelCreate={() => { setDetailOpen(false); setMode('edit'); void refreshRoster(selectedName); }}
            onClose={() => setDetailOpen(false)}
            onInstallSkill={handleInstallBotSkill}
            onDelete={handleDelete}
          />
        )
      ) : null}
      </div>
    </div>
  );
}
