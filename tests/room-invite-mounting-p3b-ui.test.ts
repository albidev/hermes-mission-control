import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

const source = (path: string) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');
test('production entry points keep pairing/invitations/guest rooms behind feature availability', () => {
  const config = source('src/routes/ConfigRoute.tsx');
  const drawer = source('src/components/ChatDrawer.tsx');
  assert.match(config, /roomInvitesAvailable\s*\?\s*\(?\s*<PairingSection/);
  assert.match(drawer, /invitesAvailable\s*\?\s*\([\s\S]*?inviteUi\.invitations/);
  assert.match(drawer, /receivedInvitationsOpen && invitesAvailable/);
  assert.match(drawer, /const selectedGuest = invitesAvailable/);
  assert.match(drawer, /!selectedGuest/);
});

test('both transcripts import the same read-only row component, without guest composer/actions', () => {
  const group = source('src/components/chat/GroupRoomView.tsx');
  const guest = source('src/components/chat/GuestRoomView.tsx');
  const row = source('src/components/chat/RoomTranscriptRow.tsx');
  assert.match(group, /<RoomTranscriptRow/);
  assert.match(guest, /<RoomTranscriptRow/);
  assert.match(row, /<ChatMessageCard/);
  assert.doesNotMatch(guest, /GroupRoomComposer|<textarea|onSend|Pause bot|window\.location\.reload/);
  assert.match(guest, /sinceSeq: sequence\.current/);
});

test('DEV mock is lazy and production guarded; translations have exact key/placeholder parity', () => {
  const app = source('src/App.tsx');
  assert.match(app, /import\.meta\.env\.DEV\s*\?\s*lazy/);
  const en = JSON.parse(source('src/locales/en.json'));
  const it = JSON.parse(source('src/locales/it.json'));
  const keys = Object.keys(en).filter((key) => key.startsWith('inviteUi.'));
  assert.ok(keys.length > 80);
  for (const key of keys) {
    assert.equal(typeof it[key], 'string', key);
    assert.deepEqual(en[key].match(/\{\w+\}/g), it[key].match(/\{\w+\}/g), key);
  }
});
