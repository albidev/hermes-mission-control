import { useEffect, useMemo, useState } from 'react';
import { createRoomInviteApi, type RoomInviteApi } from './room-invite-api';

/**
 * Whether the owner invite routes answer on this installation.
 *
 * The routes are OFF by default and then answer 404 exactly like a missing path, so the
 * entry point stays hidden until the read-only paired-peers probe succeeds. A 404, an
 * auth failure or a network error all keep it hidden; reopening the drawer, focusing the window
 * or returning to the tab probes again.
 */
export function useRoomInvitesAvailable(enabled: boolean, accessToken?: string, apiOverride?: RoomInviteApi): boolean {
  const api = useMemo(() => apiOverride ?? createRoomInviteApi(accessToken), [accessToken, apiOverride]);
  const [available, setAvailable] = useState(false);
  const [probe, setProbe] = useState(0);

  // A tab left open across a sidecar restart must notice the routes coming up (or going away).
  useEffect(() => {
    if (!enabled) return undefined;
    const reprobe = () => { if (document.visibilityState === 'visible') setProbe((value) => value + 1); };
    window.addEventListener('focus', reprobe);
    document.addEventListener('visibilitychange', reprobe);
    return () => {
      window.removeEventListener('focus', reprobe);
      document.removeEventListener('visibilitychange', reprobe);
    };
  }, [enabled]);

  useEffect(() => {
    if (!enabled) {
      setAvailable(false);
      return undefined;
    }
    const controller = new AbortController();
    api.listPairedPeers(controller.signal).then(
      () => { if (!controller.signal.aborted) setAvailable(true); },
      () => { if (!controller.signal.aborted) setAvailable(false); },
    );
    return () => controller.abort();
  }, [api, enabled, probe]);

  return enabled && available;
}
