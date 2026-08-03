import {
  type KeyboardEvent as ReactKeyboardEvent,
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from 'react';
import { ChevronRight } from 'lucide-react';

type CompletionItem = {
  display: string;
  text: string;
  meta?: string;
};

export type ChatSlashCompletionResponse = {
  items?: CompletionItem[];
  replace_from?: number;
};

export type ChatSlashPopoverHandle = {
  handleKey(event: ReactKeyboardEvent<HTMLTextAreaElement>): boolean;
};

type ChatSlashPopoverProps = {
  input: string;
  complete: (text: string) => Promise<ChatSlashCompletionResponse>;
  onApply: (nextInput: string) => void;
};

const DEBOUNCE_MS = 60;

export const ChatSlashPopover = forwardRef<ChatSlashPopoverHandle, ChatSlashPopoverProps>(
  function ChatSlashPopover({ input, complete, onApply }, ref) {
    const [items, setItems] = useState<CompletionItem[]>([]);
    const [selected, setSelected] = useState(0);
    const [replaceFrom, setReplaceFrom] = useState(0);
    const lastInputRef = useRef('');

    useEffect(() => {
      const currentInput = input ?? '';
      if (!currentInput.startsWith('/') || currentInput === lastInputRef.current) {
        if (!currentInput.startsWith('/')) lastInputRef.current = '';
        return;
      }

      lastInputRef.current = currentInput;
      const timer = window.setTimeout(async () => {
        if (lastInputRef.current !== currentInput) return;
        try {
          const response = await complete(currentInput);
          if (lastInputRef.current !== currentInput) return;
          setItems(Array.isArray(response.items) ? response.items : []);
          setReplaceFrom(typeof response.replace_from === 'number' ? response.replace_from : 0);
          setSelected(0);
        } catch {
          if (lastInputRef.current === currentInput) setItems([]);
        }
      }, DEBOUNCE_MS);

      return () => window.clearTimeout(timer);
    }, [complete, input]);

    const apply = useCallback((item: CompletionItem | undefined) => {
      if (!item) return;
      onApply(input.slice(0, replaceFrom) + item.text);
    }, [input, onApply, replaceFrom]);

    const visible = input.startsWith('/') && items.length > 0;

    useImperativeHandle(ref, () => ({
      handleKey: (event) => {
        if (!visible) return false;
        if (event.key === 'ArrowDown') {
          event.preventDefault();
          setSelected((current) => (current + 1) % items.length);
          return true;
        }
        if (event.key === 'ArrowUp') {
          event.preventDefault();
          setSelected((current) => (current - 1 + items.length) % items.length);
          return true;
        }
        if (event.key === 'Tab' || event.key === 'Enter') {
          event.preventDefault();
          apply(items[selected]);
          return true;
        }
        if (event.key === 'Escape') {
          event.preventDefault();
          setItems([]);
          return true;
        }
        return false;
      },
    }), [apply, items, selected, visible]);

    if (!visible) return null;

    return (
      <div className="chat-slash-popover" role="listbox" aria-label="Slash commands">
        <div className="chat-slash-popover-label">Commands</div>
        {items.map((item, index) => {
          const active = index === selected;
          return (
            <button
              className={`chat-slash-option ${active ? 'is-selected' : ''}`}
              key={`${item.text}-${index}`}
              type="button"
              role="option"
              aria-selected={active}
              onMouseEnter={() => setSelected(index)}
              onClick={() => apply(item)}
            >
              <ChevronRight size={13} aria-hidden />
              <span className="chat-slash-option-name">{item.display || item.text}</span>
              {item.meta ? <span className="chat-slash-option-meta">{item.meta}</span> : null}
            </button>
          );
        })}
      </div>
    );
  },
);
