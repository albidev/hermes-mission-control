import { describe, it } from 'node:test';
import { strict as assert } from 'node:assert';
import { buildBotChatHref } from '../src/lib/bot-chat-navigation.ts';
import { shouldPreviewChatSession } from '../src/lib/chat-session-params.ts';

describe('new Bot chat route', () => {
  it('scopes a fresh session to the selected profile without carrying a room or old session', () => {
    const href = buildBotChatHref('/bots', '?tab=details&chatMode=room&roomId=previous&chatSession=old', 'fresh-1', { mode: 'task', profile: 'botmaker' });
    const params = new URL(`http://localhost${href}`).searchParams;
    assert.equal(params.get('chatMode'), 'task');
    assert.equal(params.get('botProfile'), 'botmaker');
    assert.deepEqual(params.getAll('chatSession'), ['fresh-1']);
    assert.equal(params.has('roomId'), false);
    assert.equal(params.get('tab'), 'details');
  });

  it('only auto-activates the exact newly created session; existing deep links still preview', () => {
    assert.equal(shouldPreviewChatSession('new-1', 'new-1'), false);
    assert.equal(shouldPreviewChatSession('other-1', 'new-1'), true);
    assert.equal(shouldPreviewChatSession('existing-1'), true);
    assert.equal(shouldPreviewChatSession(null, 'new-1'), false);
  });
});
