import { Check, ChevronRight, Cpu, Loader2, RefreshCw, Search, X } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import type { ChatModelProviderOption, ChatModelSwitchResult } from '../lib/chat-protocol';

type GatewayRequest = <T,>(method: string, params?: Record<string, unknown>) => Promise<T>;

type ChatModelPickerProps = {
  request: GatewayRequest;
  sessionId: string | null;
  currentModel?: string;
  initialRefresh?: boolean;
  onClose: () => void;
  onSelect: (model: string, provider: string, confirmExpensiveModel?: boolean) => Promise<ChatModelSwitchResult>;
};

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function normalizeProvider(value: unknown): ChatModelProviderOption | null {
  const raw = asRecord(value);
  if (!raw || typeof raw.slug !== 'string' || !raw.slug.trim()) return null;
  const models = Array.isArray(raw.models)
    ? raw.models.filter((model): model is string => typeof model === 'string' && model.trim().length > 0)
    : [];
  return {
    slug: raw.slug.trim(),
    name: typeof raw.name === 'string' && raw.name.trim() ? raw.name.trim() : raw.slug.trim(),
    models,
    total_models: typeof raw.total_models === 'number' ? raw.total_models : models.length,
    is_current: raw.is_current === true,
    authenticated: raw.authenticated !== false,
    warning: typeof raw.warning === 'string' ? raw.warning : undefined,
  };
}

function unwrapResult(value: unknown): Record<string, unknown> {
  const raw = asRecord(value);
  if (!raw) return {};
  const nested = asRecord(raw.result);
  return nested ?? raw;
}

export function ChatModelPicker({
  request,
  sessionId,
  currentModel,
  initialRefresh = false,
  onClose,
  onSelect,
}: ChatModelPickerProps) {
  const [providers, setProviders] = useState<ChatModelProviderOption[]>([]);
  const [selectedProviderSlug, setSelectedProviderSlug] = useState('');
  const [filter, setFilter] = useState('');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [switchingModel, setSwitchingModel] = useState<string | null>(null);
  const [pendingConfirmation, setPendingConfirmation] = useState<{
    model: string;
    provider: string;
    message: string;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadOptions = async (refresh = initialRefresh) => {
    setError(null);
    if (refresh) setRefreshing(true);
    else setLoading(true);
    try {
      const raw = await request<unknown>('model.options', {
        ...(sessionId ? { session_id: sessionId } : {}),
        include_unconfigured: true,
        ...(refresh ? { refresh: true } : {}),
      });
      const payload = unwrapResult(raw);
      const next = Array.isArray(payload.providers)
        ? payload.providers.map(normalizeProvider).filter((provider): provider is ChatModelProviderOption => Boolean(provider))
        : [];
      setProviders(next);
      const current = next.find((provider) => provider.is_current)
        ?? next.find((provider) => currentModel?.startsWith(`${provider.slug}/`) || currentModel?.includes(provider.slug));
      setSelectedProviderSlug(current?.slug ?? next[0]?.slug ?? '');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load model options.');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => {
    void loadOptions();
    // The picker is intentionally loaded once per open. The refresh button is explicit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [request, sessionId]);

  const selectedProvider = providers.find((provider) => provider.slug === selectedProviderSlug) ?? providers[0] ?? null;
  const filteredModels = useMemo(() => {
    const query = filter.trim().toLowerCase();
    if (!selectedProvider) return [];
    if (!query) return selectedProvider.models;
    return selectedProvider.models.filter((model) => model.toLowerCase().includes(query));
  }, [filter, selectedProvider]);

  const choose = async (model: string, provider: string, confirmExpensiveModel = false) => {
    setSwitchingModel(model);
    setError(null);
    try {
      const result = await onSelect(model, provider, confirmExpensiveModel);
      if (result.confirmRequired) {
        setPendingConfirmation({
          model,
          provider,
          message: result.confirmMessage || result.warning || 'This model has unusually high known pricing.',
        });
        return;
      }
      if (!result.ok) {
        setError(result.error || 'Could not switch model.');
        return;
      }
      setPendingConfirmation(null);
    } finally {
      setSwitchingModel(null);
    }
  };

  return (
    <section className="chat-model-picker" aria-label="Choose Hermes model">
      <header className="chat-model-picker-head">
        <div className="chat-model-picker-title">
          <span className="chat-model-picker-icon"><Cpu size={16} aria-hidden /></span>
          <div>
            <span className="eyebrow">Runtime</span>
            <strong>Choose model</strong>
          </div>
        </div>
        <div className="chat-model-picker-actions">
          <button type="button" className="chat-model-picker-icon-button" onClick={() => void loadOptions(true)} disabled={refreshing} aria-label="Refresh model list" title="Refresh model list">
            <RefreshCw size={15} className={refreshing ? 'chat-spin' : ''} />
          </button>
          <button type="button" className="chat-model-picker-icon-button" onClick={onClose} aria-label="Close model picker" title="Close model picker">
            <X size={17} />
          </button>
        </div>
      </header>

      {currentModel ? (
        <div className="chat-model-picker-current">
          <span>ACTIVE</span>
          <strong>{currentModel}</strong>
        </div>
      ) : null}

      {loading ? (
        <div className="chat-model-picker-state"><Loader2 size={16} className="chat-spin" /> Loading providers…</div>
      ) : error && providers.length === 0 ? (
        <div className="chat-model-picker-state is-error">{error}</div>
      ) : (
        <>
          <div className="chat-model-picker-section-label">Provider</div>
          <div className="chat-model-provider-list" role="tablist" aria-label="Model providers">
            {providers.map((provider) => {
              const active = provider.slug === selectedProvider?.slug;
              const disabled = provider.authenticated === false || provider.models.length === 0;
              return (
                <button
                  key={provider.slug}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  disabled={disabled}
                  className={`chat-model-provider ${active ? 'is-selected' : ''} ${disabled ? 'is-disabled' : ''}`}
                  onClick={() => { setSelectedProviderSlug(provider.slug); setFilter(''); setPendingConfirmation(null); }}
                  title={provider.warning || provider.name}
                >
                  <span>{provider.name}</span>
                  <small>{provider.models.length || provider.total_models || 0}</small>
                </button>
              );
            })}
          </div>

          <label className="chat-model-search">
            <Search size={15} aria-hidden />
            <input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="Filter models" aria-label="Filter models" autoFocus />
          </label>

          {pendingConfirmation ? (
            <div className="chat-model-confirm" role="alert">
              <strong>Confirm model switch</strong>
              <p>{pendingConfirmation.message}</p>
              <div className="chat-model-confirm-actions">
                <button type="button" className="chat-choice" onClick={() => setPendingConfirmation(null)}>Cancel</button>
                <button type="button" className="chat-choice is-primary" onClick={() => void choose(pendingConfirmation.model, pendingConfirmation.provider, true)}>Switch anyway</button>
              </div>
            </div>
          ) : null}

          <div className="chat-model-picker-section-label">
            <span>{selectedProvider?.name || 'Models'}</span>
            <span>{filteredModels.length} available</span>
          </div>
          <div className="chat-model-list" role="listbox" aria-label="Available models">
            {filteredModels.length === 0 ? (
              <div className="chat-model-picker-state">No models match this filter.</div>
            ) : filteredModels.map((model) => {
              const active = model === currentModel;
              const switching = switchingModel === model;
              return (
                <button
                  key={`${selectedProvider?.slug}:${model}`}
                  type="button"
                  role="option"
                  aria-selected={active}
                  className={`chat-model-option ${active ? 'is-current' : ''}`}
                  disabled={Boolean(switchingModel)}
                  onClick={() => selectedProvider && void choose(model, selectedProvider.slug)}
                >
                  <span className="chat-model-option-mark">{switching ? <Loader2 size={14} className="chat-spin" /> : active ? <Check size={14} /> : <ChevronRight size={14} />}</span>
                  <span className="chat-model-option-name">{model}</span>
                  {active ? <small>current</small> : null}
                </button>
              );
            })}
          </div>
          {error ? <p className="chat-model-picker-error">{error}</p> : null}
          <p className="chat-model-picker-footnote">Selection is session-scoped. The next turn uses the new model.</p>
        </>
      )}
    </section>
  );
}
