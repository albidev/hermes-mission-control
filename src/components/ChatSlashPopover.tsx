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
import { useI18n } from '../lib/i18n';
import {
  ChatCompletionPopover,
  type ChatCompletionItem,
  type ChatCompletionPopoverHandle,
} from './ChatCompletionPopover';

export type ChatSlashCompletionResponse = {
  items?: ChatCompletionItem[];
  replace_from?: number;
};

export type ChatSlashPopoverHandle = ChatCompletionPopoverHandle;

type ChatSlashPopoverProps = {
  input: string;
  complete: (text: string) => Promise<ChatSlashCompletionResponse>;
  onApply: (nextInput: string) => void;
};

const DEBOUNCE_MS = 60;

export const ChatSlashPopover = forwardRef<ChatSlashPopoverHandle, ChatSlashPopoverProps>(
  function ChatSlashPopover({ input, complete, onApply }, ref) {
    const { t } = useI18n();
    const [items, setItems] = useState<ChatCompletionItem[]>([]);
    const [replaceFrom, setReplaceFrom] = useState(0);
    const [dismissed, setDismissed] = useState(false);
    const lastInputRef = useRef('');
    const popoverHandleRef = useRef<ChatCompletionPopoverHandle | null>(null);

    useEffect(() => {
      const currentInput = input ?? '';
      if (!currentInput.startsWith('/') || currentInput === lastInputRef.current) {
        if (!currentInput.startsWith('/')) lastInputRef.current = '';
        return;
      }

      lastInputRef.current = currentInput;
      setDismissed(false);
      const timer = window.setTimeout(async () => {
        if (lastInputRef.current !== currentInput) return;
        try {
          const response = await complete(currentInput);
          if (lastInputRef.current !== currentInput) return;
          setItems(Array.isArray(response.items) ? response.items : []);
          setReplaceFrom(typeof response.replace_from === 'number' ? response.replace_from : 0);
        } catch {
          if (lastInputRef.current === currentInput) setItems([]);
        }
      }, DEBOUNCE_MS);

      return () => window.clearTimeout(timer);
    }, [complete, input]);

    const visible = input.startsWith('/') && !dismissed && items.length > 0;

    const handleApply = useCallback((nextInput: string) => {
      onApply(nextInput);
    }, [onApply]);

    const handleDismiss = useCallback(() => {
      setDismissed(true);
      setItems([]);
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
        replaceFrom={replaceFrom}
        label={t('slash.commands')}
        ariaLabel={t('slash.commandsAria')}
        icon={<ChevronRight size={13} aria-hidden />}
        onApply={handleApply}
        onDismiss={handleDismiss}
        enterSubmits={(currentInput, item) =>
          currentInput.trim() === item.text.trim() && !currentInput.trim().includes(' ')}
      />
    );
  },
);
