import { Suspense, useCallback, useEffect, useRef, useState } from 'react';
import type { CanvasAddonDescriptor, CanvasAddonProps } from './types';
import { GenericErrorBoundary } from './GenericErrorBoundary';
import { GenericLoadingShell } from './GenericLoadingShell';

export function CanvasAddonHost({
  addon,
  ...props
}: { addon: CanvasAddonDescriptor } & CanvasAddonProps) {
  const ErrorBoundary = addon.errorBoundary ?? GenericErrorBoundary;
  const LoadingShell = addon.loadingShell ?? GenericLoadingShell;
  const [retryKey, setRetryKey] = useState(0);
  const [lifecycleTimedOut, setLifecycleTimedOut] = useState(false);
  const readyCalledRef = useRef(false);

  const onRetry = useCallback(() => {
    readyCalledRef.current = false;
    setLifecycleTimedOut(false);
    setRetryKey(k => k + 1);
  }, []);

  // Lifecycle timeout: if addon doesn't call onReady in time, show error
  useEffect(() => {
    if (props.loading) return;
    if (readyCalledRef.current) return;
    const timeout = addon.lifecycleTimeout ?? 30000;
    const timer = window.setTimeout(() => {
      if (!readyCalledRef.current) {
        setLifecycleTimedOut(true);
      }
    }, timeout);
    return () => window.clearTimeout(timer);
  }, [props.loading, addon.lifecycleTimeout]);

  // Track onReady calls from the addon
  const handleReady = useCallback(() => {
    readyCalledRef.current = true;
    props.onReady();
  }, [props.onReady]);

  if (lifecycleTimedOut && !props.loading) {
    return (
      <div className="canvas-addon-error" style={props.width ? { width: `${props.width}px` } : undefined}>
        <div className="canvas-addon-error-content">
          <p className="canvas-addon-error-title">Canvas addon timed out</p>
          <p className="canvas-addon-error-hint">{addon.label} did not become ready in time. Try reloading.</p>
          <div className="canvas-addon-error-actions">
            <button type="button" className="chat-control" onClick={onRetry}>Retry</button>
            <button type="button" className="chat-control" onClick={props.onClose}>Close</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <ErrorBoundary onClose={props.onClose} onRetry={onRetry} width={props.width}>
      <Suspense fallback={<LoadingShell onClose={props.onClose} width={props.width} />}>
        <addon.component {...props} onReady={handleReady} key={retryKey} />
      </Suspense>
    </ErrorBoundary>
  );
}
