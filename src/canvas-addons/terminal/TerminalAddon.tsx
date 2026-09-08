import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, Eraser, Loader2, PlugZap } from 'lucide-react';
import { FitAddon } from '@xterm/addon-fit';
import { Terminal as XTerm } from '@xterm/xterm';
import type { CanvasAddonProps } from '../types';
import '@xterm/xterm/css/xterm.css';

export function TerminalAddon({ storedToken, sessionTitle, onClose, onReady }: CanvasAddonProps) {
  const terminalRef = useRef<HTMLDivElement | null>(null);
  const xtermRef = useRef<XTerm | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const connectionIdRef = useRef(0);

  const [status, setStatus] = useState('Connecting…');
  const [connectionAttempt, setConnectionAttempt] = useState(0);

  useEffect(() => {
    const host = terminalRef.current;
    if (!host) return;
    const connectionId = connectionIdRef.current + 1;
    connectionIdRef.current = connectionId;

    const terminal = new XTerm({
      cursorBlink: true,
      convertEol: true,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
      fontSize: 13,
      scrollback: 5000,
      theme: {
        background: '#101116',
        foreground: '#e6e7eb',
        cursor: '#a78bfa',
      },
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(host);
    fit.fit();
    xtermRef.current = terminal;

    let disposed = false;
    const resize = () => {
      fit.fit();
      const socket = socketRef.current;
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(`\x1b[RESIZE:${terminal.cols};${terminal.rows}]`);
      }
    };
    const resizeObserver = new ResizeObserver(() => {
      resize();
    });
    resizeObserver.observe(host);

    const open = async () => {
      try {
        const ticketResponse = await fetch('/api/local/terminal/ticket', {
          method: 'POST',
          headers: { Accept: 'application/json', Authorization: `Bearer ${storedToken}` },
          credentials: 'include',
        });
        if (!ticketResponse.ok) throw new Error('Terminal authentication failed.');
        const ticketPayload = await ticketResponse.json() as { ticket?: string };
        if (!ticketPayload.ticket) throw new Error('Terminal ticket was not returned.');
        if (disposed || connectionIdRef.current !== connectionId) return;
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const socket = new WebSocket(`${protocol}//${window.location.host}/api/terminal?ticket=${encodeURIComponent(ticketPayload.ticket)}`);
        socketRef.current = socket;
        socket.onopen = () => {
          if (disposed || connectionIdRef.current !== connectionId) {
            socket.close();
            return;
          }
          setStatus('Connected');
          onReady();
          resize();
          terminal.focus();
        };
        socket.onmessage = async (event) => {
          if (disposed || connectionIdRef.current !== connectionId) return;
          if (typeof event.data === 'string') {
            terminal.write(event.data);
            return;
          }
          if (event.data instanceof ArrayBuffer) {
            terminal.write(new Uint8Array(event.data));
            return;
          }
          if (event.data instanceof Blob) {
            const data = new Uint8Array(await event.data.arrayBuffer());
            if (!disposed && connectionIdRef.current === connectionId) terminal.write(data);
          }
        };
        socket.onerror = () => {
          if (!disposed && connectionIdRef.current === connectionId) setStatus('Connection error');
        };
        socket.onclose = (event) => {
          if (connectionIdRef.current === connectionId) {
            if (socketRef.current === socket) socketRef.current = null;
            if (!disposed) setStatus(event.code ? `Disconnected (${event.code})` : 'Disconnected');
          }
        };
        terminal.onData((data) => {
          if (!disposed && connectionIdRef.current === connectionId && socket.readyState === WebSocket.OPEN) {
            socket.send(new TextEncoder().encode(data));
          }
        });
      } catch (error) {
        if (!disposed && connectionIdRef.current === connectionId) {
          setStatus(error instanceof Error ? error.message : 'Unable to connect');
          onReady();
        }
      }
    };
    void open();

    return () => {
      disposed = true;
      if (connectionIdRef.current === connectionId) connectionIdRef.current += 1;
      resizeObserver.disconnect();
      socketRef.current?.close();
      socketRef.current = null;
      terminal.dispose();
      xtermRef.current = null;
    };
  }, [connectionAttempt, onReady, storedToken]);

  const clear = () => xtermRef.current?.clear();
  const reconnect = () => {
    setStatus('Connecting…');
    setConnectionAttempt((attempt) => attempt + 1);
  };

  return (
    <section className="canvas-addon-panel terminal-addon-panel is-expanded" aria-label="Terminal">
      <header className="canvas-addon-head terminal-addon-head tldraw-canvas-head">
        <div className="canvas-addon-title tldraw-canvas-title">
          <button type="button" className="chat-icon-button tldraw-canvas-back" onClick={onClose} title="Back to chat" aria-label="Back to chat">
            <ArrowLeft size={18} />
          </button>
          <div className="canvas-addon-heading tldraw-canvas-heading">
            <span className="eyebrow">Session terminal</span>
            <h3>Terminal</h3>
            <span className="canvas-addon-linked-session tldraw-canvas-linked-session" title={sessionTitle}>{sessionTitle} · {status}</span>
          </div>
        </div>
        <div className="canvas-addon-toolbar tldraw-canvas-toolbar">
          {status === 'Connecting…' ? <Loader2 size={16} className="chat-spin" aria-label={status} /> : null}
          <button type="button" className="chat-icon-button" onClick={clear} title="Clear terminal" aria-label="Clear terminal"><Eraser size={16} /></button>
          <button type="button" className="chat-icon-button" onClick={reconnect} title="Reconnect terminal" aria-label="Reconnect terminal"><PlugZap size={16} /></button>
        </div>
      </header>
      <div className="terminal-addon-surface" ref={terminalRef} role="application" aria-label="Interactive Hermes terminal" />
    </section>
  );
}
