import { describe, it } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  BOT_REPLY_MARKER,
  buildBotReplyDelivery,
  createHandoffObserverRegistry,
} from '../src/lib/bot-reply-delivery.ts';
import { normalizeTranscript } from '../src/lib/chat-protocol.ts';

describe('buildBotReplyDelivery', () => {
  it('targets the origin captured at send time and marks the row hidden', () => {
    const payload = buildBotReplyDelivery({
      originSessionId: 'origin-session',
      replyText: 'the bot answer',
      botHandle: 'example-bot',
      requestText: 'inspect this',
    });
    assert.ok(payload);
    assert.equal(payload.session_id, 'origin-session');
    assert.equal(payload.display_kind, 'hidden');
    assert.match(payload.text, /@example-bot/);
    assert.match(payload.text, /inspect this/);
    assert.match(payload.text, /the bot answer/);
  });

  it('does NOT re-derive the session: the passed id is used verbatim', () => {
    // A settle arriving minutes after the user switched chats must still land in the
    // origin, so the id is an input, never a lookup performed at delivery time.
    const payload = buildBotReplyDelivery({
      originSessionId: '  origin-a  ',
      replyText: 'answer',
      botHandle: 'b',
      requestText: 'r',
    });
    assert.equal(payload?.session_id, 'origin-a');
  });

  it('declines an empty reply or a missing origin instead of sending an empty turn', () => {
    const base = { originSessionId: 'origin', replyText: 'x', botHandle: 'b', requestText: 'r' };
    assert.equal(buildBotReplyDelivery({ ...base, replyText: '   ' }), null);
    assert.equal(buildBotReplyDelivery({ ...base, originSessionId: '' }), null);
  });

  it('uses a marker that does NOT collide with the system-notification prefixes', () => {
    const payload = buildBotReplyDelivery({
      originSessionId: 'origin',
      replyText: 'answer',
      botHandle: 'b',
      requestText: 'r',
    });
    assert.ok(payload?.text.startsWith(BOT_REPLY_MARKER));
    // Visibility is decided by display_kind, not by the text prefix: a `[IMPORTANT:` row
    // would be reclassified as a system message (chat-protocol.ts) — this marker is not one.
    for (const prefix of ['[IMPORTANT:', '[SYSTEM:', '[System note:', '[ASYNC DELEGATION', '[BACKGROUND PROCESS']) {
      assert.ok(!payload?.text.startsWith(prefix), `must not look like ${prefix}`);
    }
  });
});

describe('createHandoffObserverRegistry', () => {
  const stub = { start: () => {}, stop: () => {} };

  it('keeps concurrent observers for a multi-mention fan-out', () => {
    const registry = createHandoffObserverRegistry();
    registry.set('handoff-1', stub);
    registry.set('handoff-2', stub);
    assert.equal(registry.size(), 2);
    assert.notEqual(registry.get('handoff-1'), undefined);
    assert.notEqual(registry.get('handoff-2'), undefined);
  });

  it('drops an entry when its handoff settles, leaving siblings untouched', () => {
    const registry = createHandoffObserverRegistry();
    registry.set('handoff-1', stub);
    registry.set('handoff-2', stub);
    assert.equal(registry.delete('handoff-1'), true);
    assert.equal(registry.get('handoff-1'), undefined);
    assert.notEqual(registry.get('handoff-2'), undefined);
    assert.equal(registry.size(), 1);
  });

  it('ignores a blank handoff id instead of creating an unreachable entry', () => {
    const registry = createHandoffObserverRegistry();
    registry.set('   ', stub);
    assert.equal(registry.size(), 0);
  });
});

describe('the delivered row stays out of the rendered transcript', () => {
  it('is dropped by normalizeTranscript on resume, so the reply renders once', () => {
    const payload = buildBotReplyDelivery({
      originSessionId: 'origin',
      replyText: 'the bot answer',
      botHandle: 'example-bot',
      requestText: 'inspect this',
    });
    assert.ok(payload);

    const rendered = normalizeTranscript([
      { role: 'user', content: payload.text, timestamp: 1, display_kind: payload.display_kind },
      { role: 'assistant', content: 'the origin agent reply', timestamp: 2 },
    ]);

    // The injected row must not come back as a user bubble: the reply is already shown by
    // the attributed bot-reply card.
    assert.equal(rendered.filter((message) => message.role === 'user').length, 0);
    assert.equal(rendered.length, 1);
    assert.equal(rendered[0].text, 'the origin agent reply');

    // Control: the SAME text without the display_kind WOULD render — proving the marker
    // alone is not what keeps it out of the transcript.
    const withoutHidden = normalizeTranscript([
      { role: 'user', content: payload.text, timestamp: 1 },
    ]);
    assert.equal(withoutHidden.length, 1);
    assert.equal(withoutHidden[0].role, 'user');
  });
});

describe('origin capture is a send-time decision, not a settle-time one', () => {
  it('delivers to the id captured at send even after the drawer moved on', () => {
    // Reproduces the defect: a settling OBSERVER can fire up to BOT_RELAY_TIMEOUT_MS
    // (~23 min) after the send, by which point "the current session" is a different chat.
    // The delivery target is therefore an INPUT captured at send time.
    const originAtSendTime = 'session-where-the-user-asked';
    const currentSessionAtSettleTime = 'some-other-chat-opened-later';

    const payload = buildBotReplyDelivery({
      originSessionId: originAtSendTime,
      replyText: 'late answer',
      botHandle: 'example-bot',
      requestText: 'the original ask',
    });

    assert.equal(payload?.session_id, originAtSendTime);
    assert.notEqual(payload?.session_id, currentSessionAtSettleTime);
  });

  it('refuses to deliver when the origin was never captured', () => {
    // A missing origin must decline loudly in the return value rather than silently
    // falling back to whatever session happens to be current.
    assert.equal(buildBotReplyDelivery({
      originSessionId: '',
      replyText: 'answer',
      botHandle: 'b',
      requestText: 'r',
    }), null);
  });
});
