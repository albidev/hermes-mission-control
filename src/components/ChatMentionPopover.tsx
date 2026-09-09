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

export type ChatMentionPopoverHandle = {
  handleKey(event: ReactKeyboardEvent<HTMLTextAreaElement>): boolean;
};

type ChatMentionPopoverProps = {
  input: string;
  roster: BotMentionCandidate[];
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  onApply: (nextInput: string) => void;
};

export const ChatMentionPopover = forwardRef<ChatMentionPopoverHandle, ChatMentionPopoverProps>(
  function ChatMentionPopover({ input, roster, textareaRef, onApply }, ref) {
    const [selected, setSelected] = useState(0);
    const [match, setMatch] = useState<ReturnType<typeof findMentionAtCaret> | null>(null);
    const lastInputRef = useRef('');

    useEffect(() => {
      const caret = textareaRef.current?.selectionStart ?? input.length;
      const next = findMentionAtCaret(input, caret, roster);
      if (input !== lastInputRef.current) {
        lastInputRef.current = input;
        setSelected(0);
      }
      setMatch(next);
    }, [input, roster, textareaRef]);

    const apply = useCallback((candidate: BotMentionCandidate | undefined) => {
      if (!candidate || !match) return;
      const replacement = `@${candidate.handle} `;
      onApply(input.slice(0, match.start) + replacement + input.slice(match.end));
    }, [input, match, onApply]);

    const visible = Boolean(match && match.matches.length > 0);

    useImperativeHandle(ref, () => ({
      handleKey: (event) => {
        if (!visible || !match) return false;
        if (event.key === 'ArrowDown') {
          event.preventDefault();
          setSelected((current) => (current + 1) % match.matches.length);
          return true;
        }
        if (event.key === 'ArrowUp') {
          event.preventDefault();
          setSelected((current) => (current - 1 + match.matches.length) % match.matches.length);
          return true;
        }
        if (event.key === 'Tab' || event.key === 'Enter') {
          event.preventDefault();
          apply(match.matches[selected]);
          return true;
        }
        if (event.key === 'Escape') {
          event.preventDefault();
          setMatch(null);
          return true;
        }
        return false;
      },
    }), [apply, match, selected, visible]);

    if (!visible || !match) return null;

    return (
      <div className="chat-slash-popover" role="listbox" aria-label="Bot mentions">
        <div className="chat-slash-popover-label">Bot</div>
        {match.matches.map((candidate, index) => {
          const active = index === selected;
          return (
            <button
              className={`chat-slash-option ${active ? 'is-selected' : ''}`}
              key={candidate.handle}
              type="button"
              role="option"
              aria-selected={active}
              onMouseEnter={() => setSelected(index)}
              onClick={() => apply(candidate)}
            >
              <AtSign size={13} aria-hidden />
              <span className="chat-slash-option-name">{candidate.displayName || candidate.handle}</span>
              <span className="chat-slash-option-meta">@{candidate.handle}{candidate.description ? ` · ${candidate.description}` : ''}</span>
            </button>
          );
        })}
      </div>
    );
  },
);
