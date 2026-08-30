import { useI18n } from '../lib/i18n';
import { useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Modal } from '../components/Modal';
import { PageHeader } from '../components/PageHeader';
import { formatTimestamp } from '../lib/format';
import { MissionControlAuthError, loadMissionControlKnowledgeFile } from '../lib/hermes-api';
import { useMissionControl } from '../lib/mission-control-store';
import { usePullToReload } from '../hooks/usePullToReload';
import { PullToReloadIndicator } from '../components/PullToReloadIndicator';

function MarkdownDetail({ content }: { content: string }) {
  return (
    <div
      className={[
        'text-sm text-text-muted leading-relaxed',
        '[&_h1]:text-lg [&_h1]:font-semibold [&_h1]:text-text [&_h1]:mt-3 [&_h1]:mb-2',
        '[&_h2]:text-base [&_h2]:font-semibold [&_h2]:text-text [&_h2]:mt-3 [&_h2]:mb-2',
        '[&_h3]:text-sm [&_h3]:font-semibold [&_h3]:text-text [&_h3]:mt-3 [&_h3]:mb-2',
        '[&_p]:mb-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:mb-2 [&_ol]:list-decimal [&_ol]:pl-5 [&_ol]:mb-2',
        '[&_li]:mb-1 [&_code]:font-mono [&_code]:text-xs [&_code]:bg-surface [&_code]:px-1 [&_code]:py-0.5 [&_code]:rounded',
        '[&_pre]:bg-surface [&_pre]:rounded [&_pre]:p-2.5 [&_pre]:overflow-x-auto [&_pre]:mb-2',
        '[&_a]:text-accent [&_a]:underline',
      ].join(' ')}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{content}</ReactMarkdown>
    </div>
  );
}

export function KnowledgeRoute() {
  const { t } = useI18n();
  const { knowledge, storedToken } = useMissionControl();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [isCompact, setIsCompact] = useState(false);
  const [detailModalOpen, setDetailModalOpen] = useState(false);
  const [fullContent, setFullContent] = useState<string>('');
  const [contentLoading, setContentLoading] = useState(false);
  const [contentError, setContentError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  const { state: pullState } = usePullToReload({
    containerRef,
    onReload: async () => {
      if (selectedItem?.sourcePath) {
        const payload = await loadMissionControlKnowledgeFile(selectedItem.sourcePath, storedToken || undefined);
        setFullContent(payload.content || t('knowledge.noPreview'));
      }
    },
  });

  const visibleSections = useMemo(
    () => knowledge.sections.filter((section) => section.id !== 'user' && section.id !== 'vault-notes'),
    [knowledge.sections],
  );

  const allItems = useMemo(() => {
    const items = visibleSections.flatMap((section) => section.items);
    const unique = new Map<string, (typeof items)[number]>();
    for (const item of items) {
      if (!unique.has(item.id)) unique.set(item.id, item);
    }
    return [...unique.values()];
  }, [visibleSections]);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    const media = window.matchMedia('(max-width: 980px)');
    const apply = () => setIsCompact(media.matches);
    apply();
    media.addEventListener('change', apply);
    return () => media.removeEventListener('change', apply);
  }, []);

  useEffect(() => {
    if (!allItems.length) {
      setSelectedId(null);
      return;
    }

    if (!selectedId || !allItems.some((item) => item.id === selectedId)) {
      setSelectedId(knowledge.primary.id || allItems[0].id);
    }
  }, [allItems, knowledge.primary.id, selectedId]);

  const selectedItem = useMemo(
    () => allItems.find((item) => item.id === selectedId) ?? knowledge.primary,
    [allItems, knowledge.primary, selectedId],
  );

  useEffect(() => {
    let cancelled = false;

    async function loadFullContent() {
      if (!selectedItem?.sourcePath) {
        setFullContent(selectedItem.contentPreview?.trim() || t('knowledge.noPreview'));
        setContentError(null);
        setContentLoading(false);
        return;
      }

      setContentLoading(true);
      setContentError(null);

      try {
        const payload = await loadMissionControlKnowledgeFile(selectedItem.sourcePath, storedToken || undefined);
        if (cancelled) return;
        setFullContent(payload.content || t('knowledge.noPreview'));
      } catch (error) {
        if (cancelled) return;
        if (error instanceof MissionControlAuthError) {
          setContentError(t('knowledge.authRequired'));
        } else {
          setContentError(error instanceof Error ? error.message : t('knowledge.failedLoad'));
        }
        setFullContent(selectedItem.contentPreview?.trim() || t('knowledge.noPreview'));
      } finally {
        if (!cancelled) {
          setContentLoading(false);
        }
      }
    }

    void loadFullContent();

    return () => {
      cancelled = true;
    };
  }, [selectedItem, storedToken, t]);

  const markdownContent = fullContent?.trim() || selectedItem.contentPreview?.trim() || t('knowledge.noPreview');

  const handleSelectItem = (itemId: string) => {
    setSelectedId(itemId);
    if (isCompact) {
      setDetailModalOpen(true);
    }
  };

  return (
    <div ref={containerRef} className="route-page-scroll flex h-full flex-col gap-5 overflow-y-auto sm:gap-6">
      <PullToReloadIndicator state={pullState} />
      <PageHeader
        eyebrow={t('knowledge.eyebrow')}
        title={t('knowledge.title')}
        description={`${t('knowledge.indexed', { count: allItems.length })} · ${knowledge.updatedAt ? t('knowledge.updated', { time: formatTimestamp(knowledge.updatedAt) }) : t('knowledge.noTimestamp')}`}
        meta={(
          <Badge variant={knowledge.available ? 'positive' : 'warning'}>
            {knowledge.available ? t('knowledge.synced') : t('knowledge.fallback')}
          </Badge>
        )}
      />

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Card padding="none" className="xl:col-span-1">
          <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
            <div className="flex flex-col gap-0.5">
              <span className="eyebrow">{t('knowledge.files')}</span>
              <h3 className="text-sm font-semibold text-text">{t('knowledge.openDetails')}</h3>
            </div>
            <span className="text-xs text-text-subtle">{t('knowledge.indexed', { count: allItems.length })}</span>
          </div>

          <div className="divide-y divide-border-subtle max-h-[760px] overflow-y-auto">
            {visibleSections.map((section) => (
              <div key={section.id} className="px-4 py-3">
                <div className="flex items-center justify-between gap-2 mb-2">
                  <p className="text-xs font-semibold uppercase tracking-wide text-text-muted">{section.title}</p>
                  <Badge variant="default">{section.items.length}</Badge>
                </div>

                <div className="flex flex-col gap-2">
                  {section.items.map((item) => {
                    const selected = selectedItem.id === item.id;
                    return (
                      <button
                        key={item.id}
                        type="button"
                        onClick={() => handleSelectItem(item.id)}
                        className={[
                          'w-full rounded-lg border p-2.5 text-left transition-colors',
                          selected
                            ? 'border-accent/40 bg-accent/10'
                            : 'border-border-subtle bg-surface hover:bg-surface-raised',
                        ].join(' ')}
                      >
                        <p className="text-xs font-medium text-text truncate">{item.title}</p>
                        <p className="text-xs text-text-muted truncate mt-1">{item.path}</p>
                        {item.updatedAt ? (
                          <p className="text-[11px] text-text-subtle mt-1">{formatTimestamp(item.updatedAt)}</p>
                        ) : null}
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </Card>

        <Card padding="none" className="xl:col-span-2 hidden xl:block">
          <div className="px-4 pt-4 pb-3 border-b border-border-subtle">
            <span className="eyebrow">{t('knowledge.detail')}</span>
            <h3 className="text-sm font-semibold text-text mt-0.5">{selectedItem.title}</h3>
            <p className="text-xs text-text-subtle mt-1">{selectedItem.path}</p>
          </div>

          <div className="p-4 flex flex-col gap-4">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-xs">
              {selectedItem.sourcePath ? (
                <div className="rounded-lg border border-border-subtle bg-surface-raised/40 px-2.5 py-2">
                  <p className="text-text-subtle uppercase tracking-wide mb-1">{t('knowledge.source')}</p>
                  <p className="text-text-muted break-words">{selectedItem.sourcePath}</p>
                </div>
              ) : null}
              {selectedItem.updatedAt ? (
                <div className="rounded-lg border border-border-subtle bg-surface-raised/40 px-2.5 py-2">
                  <p className="text-text-subtle uppercase tracking-wide mb-1">{t('knowledge.updatedLabel')}</p>
                  <p className="text-text-muted">{formatTimestamp(selectedItem.updatedAt)}</p>
                </div>
              ) : null}
            </div>

            <p className="text-sm text-text-muted">{selectedItem.excerpt || t('knowledge.noExcerpt')}</p>

            <Card variant="sunken" className="p-3 max-h-[460px] overflow-y-auto">
              {contentLoading ? <p className="text-xs text-text-subtle mb-2">{t('knowledge.loadingFull')}</p> : null}
              {contentError ? <p className="text-xs text-warning mb-2">{contentError}</p> : null}
              <MarkdownDetail content={markdownContent} />
            </Card>
          </div>
        </Card>
      </div>

      <Modal
        open={detailModalOpen}
        title={selectedItem.title}
        subtitle={selectedItem.path}
        onClose={() => setDetailModalOpen(false)}
      >
        <div className="flex flex-col gap-4">
          <div className="grid grid-cols-1 gap-2 text-xs">
            {selectedItem.sourcePath ? (
              <div className="rounded-lg border border-border-subtle bg-surface-raised/40 px-2.5 py-2">
                <p className="text-text-subtle uppercase tracking-wide mb-1">{t('knowledge.source')}</p>
                <p className="text-text-muted break-words">{selectedItem.sourcePath}</p>
              </div>
            ) : null}
            {selectedItem.updatedAt ? (
              <div className="rounded-lg border border-border-subtle bg-surface-raised/40 px-2.5 py-2">
                <p className="text-text-subtle uppercase tracking-wide mb-1">{t('knowledge.updatedLabel')}</p>
                <p className="text-text-muted">{formatTimestamp(selectedItem.updatedAt)}</p>
              </div>
            ) : null}
          </div>

          <p className="text-sm text-text-muted">{selectedItem.excerpt || t('knowledge.noExcerpt')}</p>

          <Card variant="sunken" className="p-3 max-h-[56vh] overflow-y-auto">
            {contentLoading ? <p className="text-xs text-text-subtle mb-2">{t('knowledge.loadingFull')}</p> : null}
            {contentError ? <p className="text-xs text-warning mb-2">{contentError}</p> : null}
            <MarkdownDetail content={markdownContent} />
          </Card>
        </div>
      </Modal>
    </div>
  );
}
