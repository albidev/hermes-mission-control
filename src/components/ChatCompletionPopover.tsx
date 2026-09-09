import {
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useState,
} from 'react';

export type ChatCompletionItem = {
  display: string;
  text: string;
  meta?: string;
};

export type ChatCompletionPopoverHandle = {
  handleKey(event: ReactKeyboardEvent<HTMLTextAreaElement>): boolean;
};

type ChatCompletionPopoverProps = {
  input: string;
  items: ChatCompletionItem[];
  visible: boolean;
  replaceFrom: number;
  label: string;
  ariaLabel: string;
  icon?: ReactNode;
  onApply: (nextInput: string) => void;
  onDismiss?: () => void;
  /** Return true when Enter should submit instead of applying (e.g. a full local slash command). */
  enterSubmits?: (input: string, item: ChatCompletionItem) => boolean;
};

export const ChatCompletionPopover = forwardRef<ChatCompletionPopoverHandle, ChatCompletionPopoverProps>(
  function ChatCompletionPopover({ input, items, visible, replaceFrom, label, ariaLabel, icon, onApply, onDismiss, enterSubmits }, ref) {
    const [selected, setSelected] = useState(0);

    useEffect(() => {
      setSelected(0);
    }, [items]);

    const apply = useCallback((item: ChatCompletionItem | undefined) => {
      if (!item) return;
      onApply(input.slice(0, replaceFrom) + item.text);
    }, [input, onApply, replaceFrom]);

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
          const item = items[selected];
          if (event.key === 'Enter' && item && enterSubmits?.(input, item)) {
            onDismiss?.();
            return false;
          }
          event.preventDefault();
          apply(item);
          return true;
        }
        if (event.key === 'Escape') {
          event.preventDefault();
          onDismiss?.();
          return true;
        }
        return false;
      },
    }), [apply, enterSubmits, input, items, onDismiss, selected, visible]);

    if (!visible) return null;

    return (
      <div className="chat-slash-popover" role="listbox" aria-label={ariaLabel}>
        <div className="chat-slash-popover-label">{label}</div>
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
              {icon ?? null}
              <span className="chat-slash-option-name">{item.display || item.text}</span>
              {item.meta ? <span className="chat-slash-option-meta">{item.meta}</span> : null}
            </button>
          );
        })}
      </div>
    );
  },
);
