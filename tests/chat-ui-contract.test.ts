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
const composer = readFileSync(new URL('../src/components/ChatComposer.tsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');
const buttonComponent = readFileSync(new URL('../src/components/ui/Button.tsx', import.meta.url), 'utf8');
const tldraw = readFileSync(new URL('../src/components/TLDrawCanvas.tsx', import.meta.url), 'utf8');

assertIncludes(component, 'className="chat-head-identity"', 'header groups identity and model metadata');
assertExcludes(component, '<div className="chat-head-meta">', 'header has no redundant metadata row');
assertExcludes(component, '<p className="chat-preview">{lastPreview}</p>', 'composer has no redundant last-message strip');
assertIncludes(composer, 'chat-composer-attach', 'attachment uses shared touch-target contract');
assertIncludes(composer, 'chat-composer-action chat-send', 'send uses shared touch-target contract');

assertIncludes(styles, '--chat-control-size: 44px;', 'chat control token');
assertIncludes(styles, 'height: 100dvh;', 'mobile viewport contract');
assertIncludes(styles, 'min-width: var(--chat-control-size);', 'controls keep a usable minimum width');
assertIncludes(styles, 'padding-bottom: max(0.65rem, env(safe-area-inset-bottom));', 'composer respects iPhone safe area');
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
