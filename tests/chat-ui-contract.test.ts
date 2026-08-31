import { readFileSync } from 'node:fs';

function assertIncludes(source: string, expected: string, label: string) {
  if (!source.includes(expected)) {
    throw new Error(`${label}: expected to find ${JSON.stringify(expected)}`);
  }
}

function assertExcludes(source: string, forbidden: string, label: string) {
  if (source.includes(forbidden)) {
    throw new Error(`${label}: did not expect to find ${JSON.stringify(forbidden)}`);
  }
}

const component = readFileSync(new URL('../src/components/ChatDrawer.tsx', import.meta.url), 'utf8');
const chatGateway = readFileSync(new URL('../src/lib/chat-gateway.ts', import.meta.url), 'utf8');
const composer = readFileSync(new URL('../src/components/ChatComposer.tsx', import.meta.url), 'utf8');
const messagesComponent = readFileSync(new URL('../src/components/chat-messages.tsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');
const buttonComponent = readFileSync(new URL('../src/components/ui/Button.tsx', import.meta.url), 'utf8');
const tldraw = readFileSync(new URL('../src/components/TLDrawCanvas.tsx', import.meta.url), 'utf8');
const todoPlan = readFileSync(new URL('../src/components/chat/ChatTodoPlan.tsx', import.meta.url), 'utf8');
const toolMessage = readFileSync(new URL('../src/components/chat/ToolMessage.tsx', import.meta.url), 'utf8');
const chatSync = readFileSync(new URL('../src/lib/chat-sync.ts', import.meta.url), 'utf8');

assertIncludes(component, 'className="chat-head-identity"', 'header groups identity and model metadata');
assertIncludes(component, '<ChatTodoPlan plan={visibleTodoPlan}', 'chat drawer exposes the live TODO plan');
assertIncludes(component, 'todoPlan: gatewayTodoPlan', 'chat drawer consumes the gateway TODO state');
assertIncludes(component, 'const [previewTodoPlan, setPreviewTodoPlan] = useState<TodoPlan | null>(null);', 'preview TODO state survives explicit resume');
assertIncludes(component, 'const visibleTodoPlan = gatewayTodoPlan ?? previewTodoPlan ?? derivedTodoPlan;', 'gateway TODO state has priority over preview fallback');
assertIncludes(chatGateway, "'session.events.since'", 'chat gateway replays missed session events');
assertIncludes(chatGateway, 'shouldApplySequencedEvent', 'chat gateway deduplicates sequenced events');
assertIncludes(chatGateway, 'mergeDurableChatMessages', 'chat gateway merges remote snapshots without dropping local streaming state');
assertIncludes(chatGateway, 'replay_epoch', 'chat gateway detects replay epoch changes after backend restart');
assertIncludes(chatGateway, "parsed.event.type === 'todo.updated'", 'chat gateway consumes live TODO updates');
assertIncludes(chatGateway, 'publishChatSync(storedToken, parsed.event.session_id, \'gateway_event\'', 'chat gateway mirrors core events into the sidecar relay');
assertIncludes(chatGateway, 'new EventSource(chatSyncStreamUrl', 'chat gateway subscribes to sidecar fan-out');
assertIncludes(chatGateway, 'publishChatSync(storedToken, activeSessionId, \'user_message\'', 'chat gateway mirrors user messages into the sidecar relay');
assertIncludes(chatGateway, 'publishChatSync(storedToken, activeSessionId, \'system_message\'', 'chat gateway mirrors steer acknowledgements into the sidecar relay');
assertIncludes(chatSync, 'export type ChatSyncEnvelope', 'chat sync envelope supports control acknowledgements');
assertIncludes(chatSync, 'export function applySyncedChatMessage', 'chat sync deduplicates mirrored messages');
assertIncludes(chatSync, 'let publishQueue: Promise<void> = Promise.resolve();', 'chat sync serializes publishes to preserve event order');
assertIncludes(chatGateway, "!open || !storedToken || initialSessionId?.trim()", 'explicit session resume is not overridden by the global last-chat pointer');
assertIncludes(todoPlan, 'aria-expanded={expanded}', 'TODO capsule exposes expansion state');
assertIncludes(todoPlan, 'role="progressbar"', 'expanded TODO plan exposes progress semantics');
assertIncludes(todoPlan, 'chat-plan-item-${item.status}', 'TODO plan maps item status to a semantic class');
assertIncludes(component, "className={`chat-runtime-footer ${interaction ? 'has-interaction' : ''}`}>", 'runtime footer accounts for approval and clarify surfaces');
assertIncludes(component, '{error ? (', 'runtime footer keeps errors below the TODO plan');
assertIncludes(styles, '.chat-runtime-footer.has-interaction {', 'todo plan reserves a layer above interaction surfaces');
assertIncludes(component, 'chat-status-line-ring', 'mobile status line exposes compact circular context progress');
assertIncludes(styles, '.chat-status-line-ring {', 'circular context progress has a dedicated visual treatment');
assertIncludes(styles, '.chat-status-line {\n  min-width: 0;', 'status line can shrink as one mobile row');
assertIncludes(styles, '@media (max-width: 640px)', 'chat status line has a narrow-screen layout');
assertIncludes(styles, 'flex-wrap: nowrap;', 'mobile status line keeps every metric on one row');
assertIncludes(styles, '.chat-status-line-bar {\n    display: none;', 'mobile status line replaces the linear bar with the ring');
assertIncludes(styles, '.chat-status-line-separator {\n    display: inline;', 'mobile status line keeps separators between adjacent metadata');
assertIncludes(styles, '.chat-status-line-verb {\n    flex: 0 0 auto;\n    max-width: none;\n    overflow: visible;', 'mobile status verb stays fully readable');
assertIncludes(component, 'chat-status-line-model-group', 'model and reasoning share one compact mobile group');
assertIncludes(styles, '.chat-status-line-model-group {', 'model and reasoning use one compact flex group');
assertIncludes(component, "chat-transcript ${previewMode ? 'is-preview' : ''} ${visibleTodoPlan ? 'has-todo-plan' : ''}", 'preview transcript exposes explicit TODO inset state');
assertIncludes(styles, '.chat-transcript.is-preview.has-todo-plan .chat-resume-button {\n  margin-bottom: 4.5rem;', 'mobile resume clearance does not depend on relational selectors');
assertIncludes(todoPlan, 'chat-plan-title">{statusLabel}', 'TODO capsule exposes the current plan state');
assertIncludes(todoPlan, 'expanded ? <ChevronDown size={15} /> : <ChevronUp size={15} />', 'TODO chevrons reflect collapsed and expanded state');
assertIncludes(styles, '.chat-plan {\n  position: absolute;', 'collapsed TODO plan floats above the status line');
assertIncludes(styles, '.chat-plan.is-expanded {\n  position: static;', 'expanded TODO plan returns to normal flow');
assertIncludes(styles, '.chat-plan.is-expanded .chat-plan-expanded {', 'expanded TODO plan uses the unified footer surface');
assertIncludes(styles, '.chat-transcript:has(+ .chat-runtime-footer .chat-plan:not(.is-expanded)) {', 'chat transcript reserves space for the collapsed TODO plan');
assertIncludes(styles, '.chat-transcript:has(.chat-preview-surface):has(+ .chat-runtime-footer .chat-plan:not(.is-expanded)) {\n  padding-bottom: 7rem;\n  scroll-padding-bottom: 7rem;', 'session resume preview reserves space in the scroll container');
assertIncludes(styles, '.chat-transcript:has(.chat-preview-surface):has(+ .chat-runtime-footer .chat-plan:not(.is-expanded)) .chat-resume-button {\n  margin-bottom: 4.5rem;', 'resume button keeps a direct clearance from the collapsed TODO plan');
assertIncludes(styles, 'height: 44px;', 'compact TODO capsule keeps a 44px control height');
assertIncludes(styles, '.chat-plan-capsule .chat-plan-title {', 'compact TODO capsule uses a compact status row');
assertIncludes(styles, '.chat-plan-complete.is-expanded {', 'completed expanded TODO plan keeps a uniform state border');
assertIncludes(styles, '.chat-runtime-footer {\n  position: relative;\n  z-index: 2;\n  flex: 0 0 auto;\n  background: transparent;', 'runtime footer stays transparent behind the expanded plan');
assertIncludes(styles, '.chat-plan:not(.is-expanded) .chat-plan-capsule {\n  box-shadow: 0 10px 24px rgba(0, 0, 0, 0.22);\n  background: var(--color-surface-raised);', 'collapsed TODO plan keeps its filled capsule surface');
assertIncludes(styles, '.chat-plan.is-expanded .chat-plan-expanded {\n  background: transparent;', 'expanded TODO container has no outer fill');
assertIncludes(styles, '.chat-plan.is-expanded .chat-plan-focus.is-next {', 'expanded TODO plan keeps one internal background surface');
assertIncludes(styles, '.chat-plan-expanded .chat-plan-item-pending .chat-plan-item-icon,\n.chat-plan-expanded .chat-plan-item-in_progress .chat-plan-item-icon {\n  border: 0;', 'expanded pending and running TODO dots use a single ring');
assertIncludes(styles, '.chat-plan-item-completed {\n  color: var(--color-positive);', 'completed TODO items use the positive green text color');
assertIncludes(styles, '.chat-plan:not(.is-expanded) {\n  right: auto;\n  left: 50%;\n  width: 50%;', 'collapsed TODO plan is centered and leaves room for the scroll FAB');

assertExcludes(component, '<div className="chat-head-meta">', 'header has no redundant metadata row');
assertExcludes(component, '<p className="chat-preview">{lastPreview}</p>', 'composer has no redundant last-message strip');
assertIncludes(composer, 'chat-composer-attach', 'attachment uses shared touch-target contract');
assertIncludes(composer, 'chat-composer-action chat-send', 'send uses shared touch-target contract');
assertIncludes(messagesComponent, "import { ToolMessage } from './chat/ToolMessage';", 'chat messages use the family-aware tool renderer');
assertIncludes(messagesComponent, '<ToolMessage message={message} />', 'tool messages are delegated to the family-aware renderer');
assertIncludes(messagesComponent, "const isTodoTool = isTool && message.toolName?.trim().toLowerCase() === 'todo';", 'TODO tool output is owned by the mission capsule');
assertIncludes(messagesComponent, 'if (isTodoTool) return null;', 'TODO tool stream is hidden from the transcript');
assertIncludes(toolMessage, 'export function classifyTool(toolName: string | undefined): ToolFamily', 'tool renderer classifies tool families');
assertIncludes(toolMessage, 'const FAMILY_META: Record<ToolFamily, FamilyMeta>', 'tool renderer covers the approved tool families');
assertIncludes(toolMessage, 'chat-tool-sources', 'research tools expose source links');
assertIncludes(toolMessage, 'chat-tool-payload', 'tool payloads are collapsible');
assertIncludes(toolMessage, 'function parseDelegationResults', 'delegation results are parsed from the real tool payload');
assertIncludes(toolMessage, 'function DelegationPanel', 'delegation has a dedicated live panel');
assertIncludes(toolMessage, 'chat-tool-delegation-list', 'delegation renders a per-task result list');
assertIncludes(toolMessage, 'delegationResults', 'delegation binds live results to the task panel');
assertIncludes(styles, '.chat-tool-delegation-list', 'delegation list has a dedicated visual surface');
assertIncludes(toolMessage, 'duration_seconds', 'delegation exposes child duration when provided');
assertIncludes(toolMessage, 'chat-tool-payload-chevron', 'tool payload sections use a consistent chevron control');
assertIncludes(toolMessage, 'chat-tool-section-meta', 'tool payload metadata has a dedicated alignment column');
assertIncludes(styles, 'grid-template-columns: minmax(0, 1fr) minmax(5.5rem, auto) 1rem;', 'tool payload headers align labels metadata and chevrons');
assertIncludes(styles, '.chat-tool-payload[open] .chat-tool-payload-chevron {\n  transform: rotate(180deg);', 'tool payload chevrons reflect open state');
assertExcludes(styles, ".chat-tool-payload > summary::after {\n  margin-left: auto;", 'tool payload chevrons do not rely on competing pseudo-element auto margins');
assertExcludes(toolMessage, 'browser_vision', 'tool renderer does not add screenshot handling yet');
assertIncludes(messagesComponent, 'function ChatMarkdown', 'chat messages expose one shared Markdown renderer');
assertIncludes(messagesComponent, '<ChatMarkdown', 'message cards use the shared Markdown renderer');
assertIncludes(messagesComponent, 'text={message.text}', 'streaming and completed text use Markdown renderer');
assertExcludes(messagesComponent, '<div className="chat-streaming-copy">{message.text}</div>', 'streaming text is not rendered as plain text');
assertExcludes(messagesComponent, '<ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{message.text || \'\'}</ReactMarkdown>', 'message cards do not duplicate Markdown renderer branches');


assertIncludes(styles, '--chat-control-size: 44px;', 'chat control token');
assertIncludes(styles, 'height: 100dvh;', 'mobile viewport contract');
assertIncludes(styles, 'min-width: var(--chat-control-size);', 'controls keep a usable minimum width');
assertIncludes(styles, 'padding-bottom: max(0.65rem, env(safe-area-inset-bottom));', 'composer respects iPhone safe area');
assertIncludes(styles, '.chat-markdown h1,', 'Markdown headings have explicit chat styling');
assertIncludes(styles, '.chat-markdown ul { list-style: disc; }', 'Markdown unordered lists retain markers');
assertIncludes(styles, '.chat-markdown table {', 'Markdown tables have a responsive surface');
assertExcludes(styles, '.chat-activity {', 'legacy activity strip is removed');
assertExcludes(styles, '.chat-head-meta', 'legacy metadata row styles are removed');
assertExcludes(styles, '.chat-reasoning-body', 'legacy reasoning body styles are removed');
assertExcludes(styles, '.chat-status-line-right', 'legacy status right rail is removed');
assertExcludes(styles, '.chat-attach {', 'legacy attachment selector is removed');

assertExcludes(tldraw, '<option value="">Free</option>', 'agent mode selector is deferred');
assertIncludes(tldraw, '<Tldraw colorScheme={resolvedTheme}', 'tldraw follows Mission Control theme');
assertIncludes(tldraw, 'remoteHydratedRef.current = false;', 'board identity changes reset remote hydration gate');
assertIncludes(tldraw, 'Server snapshot wins for identified sessions.', 'remote snapshot is authoritative');
assertExcludes(tldraw, 'snapshot: JSON.parse(orphan)', 'legacy local board migration cannot overwrite the server snapshot');

assertIncludes(styles, '--control-height: 44px;', 'global button control token');
assertIncludes(styles, '--touch-target: 44px;', 'global touch target token');
assertIncludes(styles, '--control-radius: 12px;', 'all visible buttons share one radius');
assertExcludes(styles, 'button:has(> svg:only-child)', 'labelled buttons are not misclassified as icon-only');
assertIncludes(styles, '@media (pointer: coarse), (max-width: 640px)', 'all mobile controls use touch targets');
assertIncludes(styles, 'min-height: var(--control-height) !important;', 'buttons use the shared minimum height');
assertIncludes(styles, 'min-width: var(--touch-target) !important;', 'buttons use the shared minimum width');
assertIncludes(styles, '.pill-button {\n    @apply min-h-11 min-w-11', 'button-like links use the shared minimum geometry');
assertIncludes(styles, '.pill-button {\n  border-radius: var(--control-radius) !important;', 'button-like links use the shared radius');
assertIncludes(buttonComponent, "sm: 'min-h-11", 'shared small buttons keep the 44px minimum');
assertExcludes(buttonComponent, "sm: 'h-7", 'shared small buttons are not fixed at 28px');
assertIncludes(
  buttonComponent,
  "secondary:\n    'bg-surface border border-border",
  'secondary actions use the neutral control surface',
);
assertIncludes(buttonComponent, "ghost:\n    'bg-surface border border-border", 'ghost actions use the same neutral surface');
assertIncludes(buttonComponent, 'iconOnly?: boolean;', 'shared button supports square icon-only controls');
assertIncludes(buttonComponent, "iconOnly ? 'mc-icon-only", 'icon-only controls expose an explicit geometry hook');
assertIncludes(styles, '.mc-icon-only {', 'icon-only controls have an explicit square contract');
assertIncludes(composer, 'chat-composer-attach', 'chat controls remain on the shared geometry contract');

const shell = readFileSync(new URL('../src/components/MissionControlShell.tsx', import.meta.url), 'utf8');
assertIncludes(shell, '<Button', 'workspace actions use the shared Button component');
assertIncludes(shell, 'iconOnly', 'workspace icon actions are explicitly square');
assertIncludes(shell, 'desktop-sidebar-toggle', 'desktop collapse control lives in the sidebar');
assertIncludes(shell, 'mobile-sidebar-toggle', 'mobile menu control stays in the primary header');
assertIncludes(shell, 'aria-expanded={!sideCollapsed}', 'desktop collapse control exposes its actual state');

console.log('chat UI contract tests passed');
