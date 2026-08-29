import { readFileSync } from 'node:fs';

const component = readFileSync(new URL('../src/components/overview/ProviderUsagePanel.tsx', import.meta.url), 'utf8');

if (!component.includes('lg:grid-cols-3')) {
  throw new Error('provider usage desktop grid must use three columns');
}
if (component.includes('lg:grid-cols-4')) {
  throw new Error('provider usage desktop grid must not use four columns');
}
if (!component.includes('featuredMetrics')) {
  throw new Error('provider usage cards must support featured metrics');
}
if (!component.includes('showReset')) {
  throw new Error('usage gauges must support provider-specific reset placement');
}
if (!component.includes('provider-reset-footer')) {
  throw new Error('Codex reset must render in a card footer');
}
if (!component.includes("provider.provider === 'codex'")) {
  throw new Error('Codex reset footer must be scoped to the Codex provider');
}

console.log('provider usage UI contract test passed');
