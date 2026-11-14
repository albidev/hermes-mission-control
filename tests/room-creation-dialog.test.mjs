import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
const drawer=readFileSync('src/components/ChatDrawer.tsx','utf8');
const css=readFileSync('src/styles.css','utf8');
const form=readFileSync('src/components/chat/GroupRoomView.tsx','utf8');
await test('creation is a drawer-root overlay, not a transcript replacement or global modal',()=>{
 assert.match(drawer,/RoomCreationLayer/);
 assert.doesNotMatch(drawer,/\) : creating \? \(/);
 assert.match(css,/\.room-create-layer\s*\{[^}]*position:\s*absolute;[^}]*inset:\s*0;/s);
 assert.match(css,/\.room-create-layer\s*\{[^}]*place-items:\s*center;/s);
});
await test('one explicit close and one local primary, scoped dialog without global modality',()=>{
 assert.match(form,/role="dialog"/);assert.doesNotMatch(form,/aria-modal/);
 assert.doesNotMatch(form,/room-create-cancel|autoFocus/);
 assert.match(form,/event\.stopPropagation\(\)/);
 assert.match(form,/disabled=\{pending\}/);
});
