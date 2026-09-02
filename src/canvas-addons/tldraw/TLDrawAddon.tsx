import type { CanvasAddonProps } from '../types';
import { TLDrawCanvas } from '../../components/TLDrawCanvas';

interface Props extends CanvasAddonProps {}

/** Wraps TLDrawCanvas to conform to the CanvasAddon contract. */
export function TLDrawAddon(props: Props) {
  return (
    <TLDrawCanvas
      sessionId={props.sessionId}
      sessionKey={props.sessionKey}
      sessionTitle={props.sessionTitle}
      storedToken={props.storedToken}
      onSendSelection={props.onSendPayload}
      onActionApplied={props.onActionApplied}
      onReady={props.onReady}
      loading={props.loading}
      onClose={props.onClose}
      expanded={props.expanded}
      width={props.width}
    />
  );
}
