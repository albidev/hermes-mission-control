import { memo, useCallback, useEffect, useRef, useState } from 'react';
import { MoreVertical, X } from 'lucide-react';
import type { CanvasAddonDescriptor, CanvasAddonId } from './types';

interface Props {
  addons: CanvasAddonDescriptor[];
  activeAddon: CanvasAddonId | null;
  onOpen: (id: CanvasAddonId) => void;
  onClose: () => void;
}

export const CanvasAddonPicker = memo(function CanvasAddonPicker({ addons, activeAddon, onOpen, onClose }: Props) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const handleClick = (event: MouseEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener('mousedown', handleClick);
    document.addEventListener('keydown', handleKey);
    return () => {
      document.removeEventListener('mousedown', handleClick);
      document.removeEventListener('keydown', handleKey);
    };
  }, [open]);

  const activeDescriptor = addons.find(a => a.id === activeAddon);

  const handleToggle = useCallback(() => {
    setOpen(prev => !prev);
  }, []);

  const handlePick = useCallback((id: CanvasAddonId) => {
    setOpen(false);
    if (id === activeAddon) {
      onClose();
    } else {
      onOpen(id);
    }
    triggerRef.current?.focus();
  }, [activeAddon, onOpen, onClose]);

  if (addons.length === 0) return null;

  return (
    <>
      {open && <div className="canvas-addon-backdrop" onClick={() => setOpen(false)} aria-hidden="true" />}

      <div className="canvas-addon-picker" ref={containerRef}>
        <button
          ref={triggerRef}
          type="button"
          className={`chat-addons-button ${activeAddon ? 'is-active' : ''} ${open ? 'is-open' : ''}`}
          onClick={handleToggle}
          title={activeDescriptor ? `${activeDescriptor.label} — click to switch or close` : 'Open canvas addon'}
          aria-label={activeDescriptor ? `Close ${activeDescriptor.label}` : 'Open canvas addon'}
          aria-expanded={open}
          aria-haspopup="true"
        >
          {activeDescriptor ? (
            <span className="chat-addons-button-addon-icon">
              {activeDescriptor.icon}
              <MoreVertical size={12} className="chat-addons-button-dots" />
            </span>
          ) : (
            <MoreVertical size={18} className="chat-addons-button-dots" />
          )}
        </button>

        {open && (
          <div className="canvas-addon-sheet" role="dialog" aria-label="Canvas addons" aria-modal="true">
            <div className="canvas-addon-sheet-header">
              <span className="canvas-addon-sheet-title">Canvas addons</span>
              <button
                type="button"
                className="canvas-addon-sheet-close"
                onClick={() => setOpen(false)}
                aria-label="Close"
              >
                <X size={18} />
              </button>
            </div>
            <ul className="canvas-addon-sheet-list">
              {addons.map(addon => (
                <li key={addon.id}>
                  <button
                    type="button"
                    className={`canvas-addon-sheet-item ${addon.id === activeAddon ? 'is-active' : ''}`}
                    onClick={() => handlePick(addon.id)}
                  >
                    <span className="canvas-addon-sheet-item-icon">{addon.icon}</span>
                    <span className="canvas-addon-sheet-item-text">
                      <span className="canvas-addon-sheet-item-label">{addon.label}</span>
                      <span className="canvas-addon-sheet-item-desc">{addon.description}</span>
                    </span>
                    {addon.id === activeAddon && (
                      <span className="canvas-addon-sheet-item-check" aria-label="Active">✓</span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </>
  );
});
