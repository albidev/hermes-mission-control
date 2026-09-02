/**
 * Canvas Addon contract.
 *
 * Every canvas addon receives these props from the host. The host handles
 * error boundaries, loading shells, and the command bridge protocol — the
 * addon only needs to render its UI and call onSendPayload when it wants
 * to contribute content to the chat.
 */
import type { ComponentType, LazyExoticComponent, ReactNode } from 'react';

export type CanvasAddonId = 'tldraw';

export type CanvasAttachment = {
  kind: 'image';
  name: string;
  mimeType: string;
  dataUrl: string;
};

export interface CanvasAddonProps {
  sessionId: string | null;
  sessionKey: string | null;
  sessionTitle: string;
  storedToken: string;
  expanded: boolean;
  width: number | null;
  loading: boolean;
  onClose: () => void;
  onReady: () => void;
  /** Send text + attachments to the chat below. */
  onSendPayload: (text: string, attachments?: CanvasAttachment[]) => Promise<boolean>;
  /** Notify actions applied (e.g. "Canvas actions applied: ..."). */
  onActionApplied?: (summary: string) => void;
}

export interface CanvasAddonDescriptor {
  id: CanvasAddonId;
  label: string;
  description: string;
  icon: ReactNode;
  /** Lazy component — must accept CanvasAddonProps. */
  component: LazyExoticComponent<ComponentType<CanvasAddonProps>>;
  /** Bridge protocol identifier (e.g. 'whiteboard-v2'). */
  bridgeProtocol?: string;
  /** Custom error boundary — falls back to GenericErrorBoundary. */
  errorBoundary?: ComponentType<{ onClose: () => void; onRetry: () => void; width: number | null }>;
  /** Custom loading shell — falls back to GenericLoadingShell. */
  loadingShell?: ComponentType<{ onClose: () => void; width: number | null }>;
  /** Lifecycle timeout in ms — host shows error if addon doesn't call onReady in time. */
  lifecycleTimeout?: number;
  /** Whether this addon supports persistence (snapshot save/load). */
  supportsPersistence?: boolean;
  /** Whether this addon supports agent commands. */
  supportsCommands?: boolean;
  /** Whether this addon supports attachments (screenshots, etc.). */
  supportsAttachments?: boolean;
  /** Whether this addon is resizable. */
  resizable?: boolean;
  /** Whether this addon supports mobile layout. */
  mobileSupport?: boolean;
}
