import assert from 'node:assert/strict';
import test from 'node:test';
import {
  classifySessionOrigin,
  getSessionActionAvailability,
  getSessionStatusMeta,
  matchesSessionFilters,
  type SessionViewFilters,
} from '../src/lib/session-view.ts';

const baseSession = {
  sessionId: 'session-123',
  title: 'Nightly memory consolidation',
  preview: '/nightly-memory-consolidation completed',
  source: 'cron',
  platform: 'cron',
  chatType: 'unknown',
  model: 'deepseek-v4-flash',
  status: 'ended' as const,
  traceMode: 'native' as const,
  category: undefined,
  originLabel: undefined,
  isResumable: undefined,
};

test('classifies human conversations and automated runs separately', () => {
  assert.deepEqual(classifySessionOrigin({ ...baseSession, source: 'tui', platform: 'tui' }), {
    category: 'conversation',
    label: 'TUI',
    resumable: true,
  });
  assert.deepEqual(classifySessionOrigin({ ...baseSession, source: 'desktop', platform: 'desktop' }), {
    category: 'conversation',
    label: 'Desktop',
    resumable: true,
  });
  assert.deepEqual(classifySessionOrigin({ ...baseSession, source: 'kanban', platform: 'kanban' }), {
    category: 'automation',
    label: 'Kanban',
    resumable: false,
  });
});

test('uses canonical backend origin metadata when available', () => {
  assert.deepEqual(classifySessionOrigin({
    ...baseSession,
    source: 'hermes-payments-p4-binding-repair',
    category: 'automation',
    originLabel: 'Worker run',
    isResumable: false,
  }), {
    category: 'automation',
    label: 'Worker run',
    resumable: false,
  });
});

test('exposes an explicit human-readable live lifecycle', () => {
  assert.equal(getSessionStatusMeta('live').label, 'LIVE');
  assert.equal(getSessionStatusMeta('idle').label, 'IDLE');
  assert.equal(getSessionStatusMeta('ended').label, 'ENDED');
  assert.equal(getSessionStatusMeta('live').tone, 'positive');
});

test('hides resume for automation and unavailable traces', () => {
  assert.deepEqual(getSessionActionAvailability({
    ...baseSession,
    status: 'live',
    source: 'cron',
    category: 'automation',
    isResumable: false,
    traceMode: 'native',
  }), { resumeChat: false, trace: true, inspect: true });
  assert.deepEqual(getSessionActionAvailability({
    ...baseSession,
    source: 'tui',
    category: 'conversation',
    isResumable: true,
    traceMode: 'unavailable',
  }), { resumeChat: true, trace: false, inspect: true });
});

test('filters by query, lifecycle, category, origin and model', () => {
  const filters: SessionViewFilters = {
    query: 'consolidation',
    status: 'ended',
    category: 'automation',
    origin: 'cron',
    model: 'deepseek-v4-flash',
  };
  assert.equal(matchesSessionFilters(baseSession, filters), true);
  assert.equal(matchesSessionFilters({ ...baseSession, status: 'live' }, filters), false);
  assert.equal(matchesSessionFilters({ ...baseSession, title: 'User chat', source: 'tui' }, filters), false);
});
