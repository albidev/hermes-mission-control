import { Suspense } from 'react';
import type { CanvasAddonDescriptor, CanvasAddonProps } from './types';
import { GenericErrorBoundary } from './GenericErrorBoundary';
import { GenericLoadingShell } from './GenericLoadingShell';

export function CanvasAddonHost({
  addon,
  ...props
}: { addon: CanvasAddonDescriptor } & CanvasAddonProps) {
  const ErrorBoundary = addon.errorBoundary ?? GenericErrorBoundary;
  const LoadingShell = addon.loadingShell ?? GenericLoadingShell;

  return (
    <ErrorBoundary onClose={props.onClose} onRetry={() => window.location.reload()} width={props.width}>
      <Suspense fallback={<LoadingShell onClose={props.onClose} width={props.width} />}>
        <addon.component {...props} />
      </Suspense>
    </ErrorBoundary>
  );
}
