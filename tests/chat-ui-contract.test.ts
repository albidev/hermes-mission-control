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
const styles = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');
const buttonComponent = readFileSync(new URL('../src/components/ui/Button.tsx', import.meta.url), 'utf8');

assertIncludes(component, 'className="chat-head-identity"', 'header groups identity and model metadata');
assertExcludes(component, '<div className="chat-head-meta">', 'header has no redundant metadata row');
assertExcludes(component, '<p className="chat-preview">{lastPreview}</p>', 'composer has no redundant last-message strip');
assertIncludes(component, 'chat-control chat-attach', 'attachment uses shared touch-target contract');
assertIncludes(component, 'chat-control chat-send', 'send uses shared touch-target contract');

assertIncludes(styles, '--chat-control-size: 44px;', 'chat control token');
assertIncludes(styles, 'height: 100dvh;', 'mobile viewport contract');
assertIncludes(styles, 'min-width: var(--chat-control-size);', 'controls keep a usable minimum width');
assertIncludes(styles, 'padding-bottom: max(0.75rem, env(safe-area-inset-bottom));', 'composer respects iPhone safe area');

assertIncludes(styles, '--control-height: 36px;', 'global desktop control token');
assertIncludes(styles, '--touch-target: 44px;', 'global touch target token');
assertIncludes(styles, 'button:has(> svg:only-child)', 'icon-only buttons use the square-control contract');
assertIncludes(styles, '@media (pointer: coarse), (max-width: 640px)', 'all mobile controls use touch targets');
assertIncludes(styles, 'min-height: var(--touch-target) !important;', 'touch targets override component-specific compact heights');
assertIncludes(styles, 'min-width: var(--touch-target) !important;', 'mobile icon buttons cannot be compressed');
assertIncludes(buttonComponent, "sm: 'min-h-9", 'shared small buttons keep the desktop minimum');
assertExcludes(buttonComponent, "sm: 'h-7", 'shared small buttons are not fixed at 28px');
assertIncludes(
  buttonComponent,
  "ghost:\n    'bg-transparent border border-border-subtle",
  'ghost actions remain visibly button-shaped',
);

console.log('chat UI contract tests passed');
