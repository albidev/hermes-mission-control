import {
  type KeyboardEvent as ReactKeyboardEvent,
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type RefObject,
} from 'react';
import { AtSign } from 'lucide-react';
import { findMentionAtCaret, type BotMentionCandidate } from '../lib/bot-mentions';
import {
  ChatCompletionPopover,
  type ChatCompletionItem,
  type ChatCompletionPopoverHandle,
} from './ChatCompletionPopover';

export type ChatMentionPopoverHandle = ChatCompletionPopoverHandle;

type ChatMentionPopoverProps = {
  input: string;
  roster: BotMentionCandidate[];
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  onApply: (nextInput: string) => void;
};

export const ChatMentionPopover = forwardRef<ChatMentionPopoverHandle, ChatMentionPopoverProps>(
  function ChatMentionPopover({ input, roster, textareaRef, onApply }, ref) {
    const [match, setMatch] = useState<ReturnType<typeof findMentionAtCaret> | null>(null);
    const [dismissed, setDismissed] = useState(false);
    const lastInputRef = useRef('');
    const popoverHandleRef = useRef<ChatCompletionPopoverHandle | null>(null);

    useEffect(() => {
      const caret = textareaRef.current?.selectionStart ?? input.length;
      const next = findMentionAtCaret(input, caret, roster);
      if (input !== lastInputRef.current) {
        lastInputRef.current = input;
        setDismissed(false);
      }
      setMatch(next);
    }, [input, roster, textareaRef]);

    const items: ChatCompletionItem[] = (match?.matches ?? []).map((candidate) => ({
      display: candidate.displayName || candidate.handle,
      text: `@${candidate.handle} `,
      meta: `@${candidate.handle}${candidate.description ? ` · ${candidate.description}` : ''}`,
    }));

    const visible = Boolean(match && items.length > 0 && !dismissed);

    const handleApply = useCallback((nextInput: string) => {
      onApply(nextInput);
    }, [onApply]);

    const handleDismiss = useCallback(() => {
      setDismissed(true);
      setMatch(null);
    }, []);

    useImperativeHandle(ref, () => ({
      handleKey: (event) => {
        const handle = popoverHandleRef.current;
        return handle ? handle.handleKey(event) : false;
      },
    }), []);

    return (
      <ChatCompletionPopover
        ref={popoverHandleRef}
        input={input}
        items={items}
        visible={visible}
        replaceFrom={match?.start ?? 0}
        label="Bot"
        ariaLabel="Bot mentions"
        icon={<AtSign size={13} aria-hidden />}
        onApply={handleApply}
        onDismiss={handleDismiss}
      />
    );
  },
);
