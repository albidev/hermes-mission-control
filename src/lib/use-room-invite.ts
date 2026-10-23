import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoomInviteApi, RoomInviteApiError, type RoomInviteActivationStatus, type RoomInviteApi, type RoomInviteError, type RoomInviteIssue, type RoomInviteLobby, type RoomInviteBinding } from './room-invite-api';

type LobbyState =
  | { status: 'idle' | 'loading'; lobby: null; activation: null; error: null }
  | { status: 'empty'; lobby: null; activation: null; error: null }
  | { status: 'ready'; lobby: RoomInviteLobby; activation: RoomInviteActivationStatus | null; error: null }
  | { status: 'error'; lobby: RoomInviteLobby | null; activation: RoomInviteActivationStatus | null; error: RoomInviteError };

export type UseRoomInviteResult = LobbyState & {
  reload: () => Promise<void>;
  resume: (lobbyId: string) => Promise<void>;
  issueInvite: (input: { member_id: string; invitee_label: string; ttl_seconds?: number }) => Promise<RoomInviteIssue>;
  revokeInvite: (inviteId: string) => Promise<void>;
  decideHost: (inviteId: string, peerId: string, approved: boolean, expectedBinding: RoomInviteBinding) => Promise<void>;
  activate: () => Promise<{ lobby_id: string; status: 'activated'; room_id: string }>;
  refreshActivation: () => Promise<void>;
  isMutating: boolean;
  activationNeedsRefresh: boolean;
};

function asError(error: unknown): RoomInviteError {
  if (error instanceof RoomInviteApiError) return error.problem;
  if (error instanceof DOMException && error.name === 'AbortError') return { kind: 'error', code: 'aborted' };
  return { kind: 'error', code: 'network_error' };
}

export function useRoomInvite(lobbyId: string | null, accessToken?: string, apiOverride?: RoomInviteApi): UseRoomInviteResult {
  const api = useMemo(() => apiOverride ?? createRoomInviteApi(accessToken), [accessToken, apiOverride]);
  const [state, setState] = useState<LobbyState>({ status: 'idle', lobby: null, activation: null, error: null });
  const [isMutating, setIsMutating] = useState(false);
  const [activationNeedsRefresh, setActivationNeedsRefresh] = useState(false);
  const loadControllerRef = useRef<AbortController | null>(null);
  const mutationControllersRef = useRef(new Set<AbortController>());
  const mountedRef = useRef(true);
  const currentLobbyRef = useRef(lobbyId);
  const load = useCallback(async (id: string) => {
    loadControllerRef.current?.abort();
    const controller = new AbortController();
    loadControllerRef.current = controller;
    currentLobbyRef.current = id;
    setState({ status: 'loading', lobby: null, activation: null, error: null });
    try {
      const lobby = await api.getLobby(id, controller.signal);
      const activation = await api.getActivation(id, controller.signal);
      if (!mountedRef.current || controller.signal.aborted || currentLobbyRef.current !== id) return;
      setActivationNeedsRefresh(false);
      if (lobby.roster.length === 0) setState({ status: 'empty', lobby: null, activation: null, error: null });
      else setState({ status: 'ready', lobby, activation, error: null });
    } catch (error) {
      if (!mountedRef.current || controller.signal.aborted || currentLobbyRef.current !== id) return;
      const problem = asError(error);
      setState({ status: 'error', lobby: null, activation: null, error: problem });
    }
  }, [api]);

  useEffect(() => {
    currentLobbyRef.current = lobbyId;
    if (lobbyId) void load(lobbyId);
    else { loadControllerRef.current?.abort(); setState({ status: 'idle', lobby: null, activation: null, error: null }); }
  }, [lobbyId, load]);

  useEffect(() => () => {
    mountedRef.current = false;
    loadControllerRef.current?.abort();
    for (const controller of mutationControllersRef.current) controller.abort();
  }, []);

  const mutate = useCallback(async (operation: (signal: AbortSignal) => Promise<void>) => {
    const id = currentLobbyRef.current;
    if (!id) throw new RoomInviteApiError({ kind: 'error', code: 'no_lobby' });
    const controller = new AbortController();
    mutationControllersRef.current.add(controller);
    setIsMutating(true);
    try {
      await operation(controller.signal);
      if (mountedRef.current && currentLobbyRef.current === id) await load(id);
    } catch (error) {
      if (!controller.signal.aborted && mountedRef.current && currentLobbyRef.current === id) {
        const problem = asError(error);
        if (problem.kind === 'ambiguous') setActivationNeedsRefresh(true);
        if (problem.code === 'binding_changed') {
          try {
            const activation = await api.getActivation(id);
            if (mountedRef.current && currentLobbyRef.current === id) {
              setActivationNeedsRefresh(false);
              setState((previous) => previous.lobby ? { status: 'error', lobby: previous.lobby, activation, error: problem } : previous);
            }
          } catch (refreshError) {
            if (mountedRef.current && currentLobbyRef.current === id) {
              setState((previous) => ({ ...previous, status: 'error', error: asError(refreshError) }));
            }
          }
          throw error;
        }
        setState((previous) => ({ ...previous, status: 'error', error: problem }));
      }
      throw error;
    } finally {
      mutationControllersRef.current.delete(controller);
      setIsMutating(mutationControllersRef.current.size > 0);
    }
  }, [load]);

  const refreshActivation = useCallback(async () => {
    const id = currentLobbyRef.current; if (!id) return;
    const controller = new AbortController();
    try {
      const activation = await api.getActivation(id, controller.signal);
      if (mountedRef.current && currentLobbyRef.current === id) {
        setActivationNeedsRefresh(false);
        setState((previous) => previous.lobby ? { status: 'ready', lobby: previous.lobby, activation, error: null } : previous);
      }
    } catch (error) {
      if (mountedRef.current && currentLobbyRef.current === id) setState((previous) => ({ ...previous, status: 'error', error: asError(error) }));
    }
  }, [api]);

  const issueInvite = useCallback(async (input: { member_id: string; invitee_label: string; ttl_seconds?: number }) => {
    const id = currentLobbyRef.current;
    if (!id) throw new RoomInviteApiError({ kind: 'error', code: 'no_lobby' });
    const controller = new AbortController();
    mutationControllersRef.current.add(controller); setIsMutating(true);
    try {
      const issued = await api.issueInvite(id, input, controller.signal);
      if (mountedRef.current && currentLobbyRef.current === id) await load(id);
      return issued;
    } catch (error) {
      if (!controller.signal.aborted && mountedRef.current && currentLobbyRef.current === id) setState((previous) => ({ ...previous, status: 'error', error: asError(error) }));
      throw error;
    } finally {
      mutationControllersRef.current.delete(controller); setIsMutating(mutationControllersRef.current.size > 0);
    }
  }, [api, load]);

  const revokeInvite = useCallback((inviteId: string) => mutate((signal) => api.revokeInvite(currentLobbyRef.current as string, inviteId, signal).then(() => undefined)), [api, mutate]);
  const decideHost = useCallback((inviteId: string, peerId: string, approved: boolean, expectedBinding: RoomInviteBinding) => mutate((signal) => api.decideHost(currentLobbyRef.current as string, inviteId, { peer_id: peerId, approved, expected_binding: expectedBinding }, signal).then(() => undefined)), [api, mutate]);
  const activate = useCallback(async () => {
    const id = currentLobbyRef.current;
    if (!id) throw new RoomInviteApiError({ kind: 'error', code: 'no_lobby' });
    const controller = new AbortController();
    mutationControllersRef.current.add(controller); setIsMutating(true);
    try {
      const result = await api.activate(id, controller.signal);
      if (!mountedRef.current || controller.signal.aborted || currentLobbyRef.current !== id) return result;
      await load(id); return result;
    } catch (error) {
      if (!controller.signal.aborted && mountedRef.current && currentLobbyRef.current === id) {
        const problem = asError(error); if (problem.kind === 'ambiguous') setActivationNeedsRefresh(true);
        setState((previous) => ({ ...previous, status: 'error', error: problem }));
      }
      throw error;
    } finally {
      mutationControllersRef.current.delete(controller); setIsMutating(mutationControllersRef.current.size > 0);
    }
  }, [api, load]);

  return { ...state, reload: () => currentLobbyRef.current ? load(currentLobbyRef.current) : Promise.resolve(), resume: load, issueInvite, revokeInvite, decideHost, activate, refreshActivation, isMutating, activationNeedsRefresh };
}