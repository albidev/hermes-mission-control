import { useCallback, useEffect, useRef, useState } from 'react';
import { markChatPresenceRead } from '../lib/chat-presence';
import type { ChatMessage } from '../lib/chat-protocol';

type UseChatTranscriptScrollProps = {
  messages: ChatMessage[];
  open: boolean;
  sessionKey: string | null;
};

export function useChatTranscriptScroll({ messages, open, sessionKey }: UseChatTranscriptScrollProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [nearBottom, setNearBottom] = useState(true);
  const nearBottomRef = useRef(true);
  const programmaticScrollRef = useRef(false);
  const scrollFrameRef = useRef<number | null>(null);

  const isNearBottom = useCallback(() => {
    const element = scrollRef.current;
    if (!element) return true;
    return element.scrollHeight - element.scrollTop - element.clientHeight < 80;
  }, []);

  const handleTranscriptScroll = useCallback(() => {
    const element = scrollRef.current;
    if (!element) return;
    const bottom = isNearBottom();
    if (programmaticScrollRef.current) {
      if (bottom) programmaticScrollRef.current = false;
      else return;
    }
    if (bottom && open) {
      const assistantCount = messages.filter((message) => message.role === 'assistant' && message.status !== 'streaming').length;
      markChatPresenceRead(sessionKey, assistantCount);
    }
    nearBottomRef.current = bottom;
    setNearBottom(bottom);
  }, [isNearBottom, messages, open, sessionKey]);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    programmaticScrollRef.current = true;
    if (scrollFrameRef.current !== null) window.cancelAnimationFrame(scrollFrameRef.current);
    const element = scrollRef.current;
    if (!element) {
      programmaticScrollRef.current = false;
      return;
    }
    if (behavior === 'auto') {
      scrollFrameRef.current = null;
      element.scrollTo({ top: element.scrollHeight, behavior: 'auto' });
      programmaticScrollRef.current = false;
      return;
    }
    scrollFrameRef.current = window.requestAnimationFrame(() => {
      scrollFrameRef.current = null;
      const current = scrollRef.current;
      if (!current) {
        programmaticScrollRef.current = false;
        return;
      }
      current.scrollTo({ top: current.scrollHeight, behavior: 'smooth' });
      programmaticScrollRef.current = false;
    });
  }, []);

  useEffect(() => () => {
    if (scrollFrameRef.current !== null) window.cancelAnimationFrame(scrollFrameRef.current);
    scrollFrameRef.current = null;
    programmaticScrollRef.current = false;
  }, []);

  useEffect(() => {
    if (!open || !nearBottomRef.current) return;
    scrollToBottom('auto');
  }, [messages, open, scrollToBottom]);

  return { scrollRef, nearBottom, nearBottomRef, setNearBottom, handleTranscriptScroll, scrollToBottom };
}
