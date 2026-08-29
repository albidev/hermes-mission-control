import { readFileSync } from 'node:fs';

const component = readFileSync(new URL('../src/components/overview/ProviderUsagePanel.tsx', import.meta.url), 'utf8');

if (!component.includes('lg:grid-cols-3')) {
  throw new Error('provider usage desktop grid must use three columns');
}
if (component.includes('lg:grid-cols-4')) {
  throw new Error('provider usage desktop grid must not use four columns');
}

console.log('provider usage UI contract test passed');
