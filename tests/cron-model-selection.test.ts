import assert from 'node:assert/strict';
import test from 'node:test';
import {
  cronModelOptions,
  cronProviderOptions,
  isCronModelPairValid,
  modelSelectionPayload,
  type CronModelProviderOption,
} from '../src/lib/cron-model-selection.ts';

const providers: CronModelProviderOption[] = [
  { slug: 'openai-codex', name: 'OpenAI Codex', models: ['gpt-6-luna-900k', 'gpt-5.6-terra'] },
  { slug: 'nous', name: 'Nous Portal', models: ['meituan/longcat-2.0:free'] },
];

test('cron provider options include inherit and retain an unavailable current provider', () => {
  assert.deepEqual(cronProviderOptions(providers, 'retired-provider', 'Use Hermes default'), [
    { value: '', label: 'Use Hermes default' },
    { value: 'openai-codex', label: 'OpenAI Codex (openai-codex)' },
    { value: 'nous', label: 'Nous Portal (nous)' },
    { value: 'retired-provider', label: 'retired-provider' },
  ]);
});

test('cron model options are provider-scoped and preserve an unavailable current model', () => {
  assert.deepEqual(cronModelOptions(providers, 'openai-codex', 'gpt-6-luna-900k', 'Use provider default'), [
    { value: '', label: 'Use provider default' },
    { value: 'gpt-6-luna-900k', label: 'gpt-6-luna-900k' },
    { value: 'gpt-5.6-terra', label: 'gpt-5.6-terra' },
  ]);
  assert.deepEqual(cronModelOptions(providers, 'nous', 'retired-model', 'Use provider default'), [
    { value: '', label: 'Use provider default' },
    { value: 'meituan/longcat-2.0:free', label: 'meituan/longcat-2.0:free' },
    { value: 'retired-model', label: 'retired-model' },
  ]);
});

test('cron model payload clears both pin fields or sends the explicit pair together', () => {
  assert.deepEqual(modelSelectionPayload('  ', ''), { model: null, provider: null });
  assert.deepEqual(modelSelectionPayload(' gpt-6-luna-900k ', ' openai-codex '), {
    model: 'gpt-6-luna-900k',
    provider: 'openai-codex',
  });
});

test('cron model pin requires provider and model to be selected together', () => {
  assert.equal(isCronModelPairValid('', ''), true);
  assert.equal(isCronModelPairValid('gpt-6-luna-900k', 'openai-codex'), true);
  assert.equal(isCronModelPairValid('gpt-6-luna-900k', ''), false);
  assert.equal(isCronModelPairValid('', 'openai-codex'), false);
});
