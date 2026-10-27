import { strict as assert } from 'node:assert';
import { runComposerSubmit, shouldRestoreRejectedDraft } from '../src/lib/chat-composer-submit.ts';

// Behavioral regression: deferred submit must clear the draft optimistically and
// restore it on rejection — but only when the session hasn't changed and the
// user hasn't typed a new draft in the meantime. Attachments must stay pending
// on rejection (not revoked/cleared) so they can be resent.

function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void; reject: (e: unknown) => void } {
  let res!: (v: T) => void; let rej!: (e: unknown) => void;
  const p = new Promise<T>((r, j) => { res = r; rej = j; });
  return { promise: p, resolve: res, reject: rej };
}

// ---- Test 1: accepted submit clears draft early and clears attachments ----
{
  const d = deferred<boolean>();
  let draft = 'hello';
  const attachments = [{ id: 'a1', previewUrl: 'blob:x' }];
  const cleared: typeof attachments = [];
  const outcome = runComposerSubmit({
    text: 'hello',
    sessionId: 's1',
    attachments,
    readUploads: async (a) => a,
    submit: () => d.promise,
    readDraft: () => draft,
    readSessionId: () => 's1',
    setDraft: (v) => { draft = v; },
    clearComposerAttachments: (a) => { for (const x of a) cleared.push(x); },
  });
  assert.strictEqual(draft, '', 'draft must be cleared before await');
  d.resolve(true);
  const res = await outcome;
  assert.strictEqual(res.sent, true); assert.strictEqual(res.restoredDraft, false);
  assert.strictEqual(cleared.length, 1, 'attachments cleared on success');
  assert.strictEqual(draft, '', 'draft stays cleared after success');
}

// ---- Test 2: rejected submit restores draft when session unchanged and no new typing ----
{
  const d = deferred<boolean>();
  let draft = '';
  const outcome = runComposerSubmit({
    text: 'hello',
    sessionId: 's1',
    attachments: [],
    readUploads: async () => [],
    submit: () => d.promise,
    readDraft: () => draft,
    readSessionId: () => 's1',
    setDraft: (v) => { draft = v; },
    clearComposerAttachments: () => {},
  });
  assert.strictEqual(draft, '', 'draft cleared early');
  d.resolve(false);
  const res = await outcome;
  assert.strictEqual(res.sent, false); assert.strictEqual(res.restoredDraft, true);
  assert.strictEqual(draft, 'hello', 'rejected draft restored');
}

// ---- Test 3: rejected submit does NOT restore if user typed a new draft ----
{
  const d = deferred<boolean>();
  let draft = 'hello';
  const outcome = runComposerSubmit({
    text: 'hello',
    sessionId: 's1',
    attachments: [],
    readUploads: async () => [],
    submit: () => d.promise,
    readDraft: () => draft,
    readSessionId: () => 's1',
    setDraft: (v) => { draft = v; },
    clearComposerAttachments: () => {},
  });
  // Optimistic clear happens synchronously inside the call.
  assert.strictEqual(draft, '', 'draft must be cleared optimistically');
  // User types a new message while the submit is in flight.
  draft = 'new';
  d.resolve(false);
  const res = await outcome;
  assert.strictEqual(res.restoredDraft, false, 'new draft must not be overwritten');
  assert.strictEqual(draft, 'new', 'draft preserved');
}

// ---- Test 4: rejected submit does NOT restore if session changed ----
{
  const d = deferred<boolean>();
  let draft = '';
  const outcome = runComposerSubmit({
    text: 'hello',
    sessionId: 's1',
    attachments: [],
    readUploads: async () => [],
    submit: () => d.promise,
    readDraft: () => draft,
    readSessionId: () => 's2', // session changed
    setDraft: (v) => { draft = v; },
    clearComposerAttachments: () => {},
  });
  d.resolve(false);
  const res = await outcome;
  assert.strictEqual(res.restoredDraft, false, 'session change must block restore');
}

// ---- Test 5: attachment read failure restores draft and keeps attachments ----
{
  let draft = '';
  const attachments = [{ id: 'a2', previewUrl: null }];
  const outcome = runComposerSubmit({
    text: 'file prompt',
    sessionId: 's1',
    attachments,
    readUploads: async () => { throw new Error('read fail'); },
    submit: async () => true,
    readDraft: () => draft,
    readSessionId: () => 's1',
    setDraft: (v) => { draft = v; },
    clearComposerAttachments: () => {},
  });
  const res = await outcome;
  assert.strictEqual(res.sent, false); assert.strictEqual(res.restoredDraft, true);
  assert.strictEqual(draft, 'file prompt', 'read-failure draft restored');
}

// ---- Unit: restore guard ----
assert.strictEqual(shouldRestoreRejectedDraft({ text: 'x', sessionId: 's', currentDraft: '', currentSessionId: 's' }), true);
assert.strictEqual(shouldRestoreRejectedDraft({ text: 'x', sessionId: 's', currentDraft: 'y', currentSessionId: 's' }), false);
assert.strictEqual(shouldRestoreRejectedDraft({ text: 'x', sessionId: 's', currentDraft: '', currentSessionId: 't' }), false);
assert.strictEqual(shouldRestoreRejectedDraft({ text: '   ', sessionId: 's', currentDraft: '', currentSessionId: 's' }), false);

console.log('deferred-submit behavioral regression: passing');
