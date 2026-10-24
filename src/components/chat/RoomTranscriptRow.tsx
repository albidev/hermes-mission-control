import type { ReactNode } from 'react';
import type { ChatMessage } from '../../lib/chat-protocol';
import { ChatMessageCard } from '../chat-messages';

type RoomTranscriptRowProps = {
  message: ChatMessage;
  mentionHandles?: string[];
  eventId?: string;
  sequence?: number;
  children?: ReactNode;
};

/** Presentation only: neither a gateway state dependency nor a composer/action surface. */
export function RoomTranscriptRow({ message, mentionHandles, eventId, sequence, children }: RoomTranscriptRowProps) {
  return (
    <div className="room-transcript-row" data-event-id={eventId ?? message.id} data-seq={sequence}>
      {children}
      {sequence !== undefined ? <p className="mb-1 text-xs text-text-subtle">#{sequence}</p> : null}
      <ChatMessageCard message={message} mentionHandles={mentionHandles} />
    </div>
  );
}
