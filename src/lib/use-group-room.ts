import { useCallback, useEffect, useRef, useState } from 'react';
import {
  GroupGatewayClient,
  type GroupActor,
  type GroupCapabilities,
  type GroupEvent,
  type GroupGatewayClient as GroupGatewayClientType,
  type GroupMember,
  type GroupRoom,
  type GroupState,
} from './group-gateway';

export type RoundCoordinates = { round: number; coordinates: Array<{ x: number; y: number }> };
export type GroupRoomError = { message: string; retryable: boolean };
export type GroupRoomOptions = {
  client?: GroupGatewayClientType;
  pollMs?: number;
  initialRoomId?: string | null;
  enabled?: boolean;
};
export type GroupRoomResult = {
  capabilities: GroupCapabilities | null;
  driverAvailable: boolean;
  rooms: GroupRoom[];
  room: GroupRoom | null;
  selectedRoomId: string | null;
  events: GroupEvent[];
  cursor: string | number | null;
  loading: boolean;
  refreshing: boolean;
  error: GroupRoomError | null;
  serviceUnavailable: boolean;
  authorityChanged: boolean;
  driverStatus: Record<string, unknown>;
  pendingActions: unknown[];
  approval: unknown;
  blocked: boolean;
  working: boolean;
  disbanded: boolean;
  memberActivity: Record<string, GroupEvent>;
  round: RoundCoordinates;
  focusHandle: string | null;
  selectRoom: (roomId: string | null) => Promise<void>;
  refresh: () => Promise<void>;
  retry: () => Promise<void>;
  send: (text: string, threadId?: string) => Promise<GroupEvent | null>;
  disband: () => Promise<void>;
  approve: (params: { memberId?: string | null; taskId?: string | null; executionGeneration?: number; choice?: string | null; requestId?: string | null }) => Promise<unknown>;
  retryMember: (taskId?: string | null) => Promise<unknown>;
  renameRoom: (name: string) => Promise<void>;
  stopRoom: () => Promise<number | null>;
  clearAuthorityChange: () => void;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

export function mergeGroupEvents(existing: GroupEvent[], incoming: GroupEvent[]): GroupEvent[] {
  const byIdentity = new Map<string, GroupEvent>();
  for (const item of existing) byIdentity.set(item.id ? `id:${item.id}` : `seq:${item.seq}`, item);
  for (const item of incoming) {
    const idKey = item.id ? `id:${item.id}` : null;
    const seqKey = `seq:${item.seq}`;
    if (idKey && byIdentity.has(idKey)) byIdentity.set(idKey, item);
    else {
      for (const [key, value] of byIdentity) if (value.seq === item.seq) byIdentity.delete(key);
      byIdentity.set(idKey ?? seqKey, item);
    }
  }
  return [...byIdentity.values()].sort((a, b) => a.seq - b.seq || a.id.localeCompare(b.id));
}

export function mapActorMember(actor: GroupActor, members: GroupMember[]): GroupMember | null {
  if (actor.kind !== 'member') return null;
  return members.find((member) => member.id === actor.id) ?? null;
}

function numeric(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

export function deriveRoundCoordinates(events: GroupEvent[]): RoundCoordinates {
  let round = 0;
  const coordinates: Array<{ x: number; y: number }> = [];
  for (const event of events) {
    const item = event as GroupEvent & { round?: unknown; coordinates?: unknown; payload?: unknown };
    const payload = isRecord(item.payload) ? item.payload : {};
    const candidateRound = numeric(item.round) ?? numeric(payload.round);
    if (candidateRound !== null) round = Math.max(round, candidateRound);
    const point = isRecord(item.coordinates) ? item.coordinates : isRecord(payload.coordinates) ? payload.coordinates : null;
    const x = point ? numeric(point.x) : null;
    const y = point ? numeric(point.y) : null;
    if (x !== null && y !== null) coordinates.push({ x, y });
  }
  return { round, coordinates };
}

export function toGroupRoomError(cause: unknown): GroupRoomError {
  const message = cause instanceof Error ? cause.message : String(cause);
  return { message, retryable: true };
}

function statusParts(status: Record<string, unknown>) {
  const pending = Array.isArray(status.pending_actions) ? status.pending_actions : [];
  const approval = status.approval ?? status.pending_approval ?? null;
  return { pendingActions: pending, approval, blocked: status.blocked === true || status.status === 'blocked' };
}

export function useGroupRoom(options: GroupRoomOptions = {}): GroupRoomResult {
  const clientRef = useRef(options.client ?? new GroupGatewayClient());
  const [capabilities, setCapabilities] = useState<GroupCapabilities | null>(null);
  const [rooms, setRooms] = useState<GroupRoom[]>([]);
  const [room, setRoom] = useState<GroupRoom | null>(null);
  const [selectedRoomId, setSelectedRoomId] = useState<string | null>(options.initialRoomId ?? null);
  const [events, setEvents] = useState<GroupEvent[]>([]);
  const [cursor, setCursor] = useState<string | number | null>(null);
  const [driverStatus, setDriverStatus] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<GroupRoomError | null>(null);
  const [authorityChanged, setAuthorityChanged] = useState(false);
  const mountedRef = useRef(true);
  const authorityRef = useRef<{ gatewayId: string; epoch: number } | null>(null);

  const loadRooms = useCallback(async () => {
    const result = await clientRef.current.list();
    if (mountedRef.current) setRooms(result.rooms);
    return result.rooms;
  }, []);

  const applyLog = useCallback((page: { events: GroupEvent[]; cursor: string | number | null; latestSeq: number; authority: { gatewayId: string; epoch: number } | null }) => {
    if (!mountedRef.current) return;
    let replacedAuthority = false;
    if (page.authority) {
      const previous = authorityRef.current;
      if (previous && (previous.epoch !== page.authority.epoch || previous.gatewayId !== page.authority.gatewayId)) {
        setAuthorityChanged(true);
        replacedAuthority = true;
        setCursor(null);
      }
      authorityRef.current = page.authority;
    }
    setEvents((current) => replacedAuthority ? page.events : mergeGroupEvents(current, page.events));
    setCursor(page.cursor);
    setRoom((current) => current ? { ...current, latestSeq: Math.max(current.latestSeq, page.latestSeq) } : current);
  }, []);

  const loadRoom = useCallback(async (roomId: string, reset = false) => {
    const state: GroupState = await clientRef.current.state(roomId);
    if (!mountedRef.current) return;
    setRoom(state.room);
    setDriverStatus(state.driverStatus ?? {});
    if (reset) {
      setEvents([]);
      setCursor(null);
      authorityRef.current = { gatewayId: state.room.authorityGatewayId, epoch: state.room.authorityEpoch };
    }
    let page = await clientRef.current.log(roomId, reset ? { sinceSeq: 0 } : { ...(cursor === null ? { sinceSeq: state.room.latestSeq } : { cursor }) });
    applyLog(page);
    while (page.hasMore && page.cursor !== null) {
      page = await clientRef.current.log(roomId, { cursor: page.cursor });
      applyLog(page);
    }
  }, [applyLog, cursor]);

  const selectRoom = useCallback(async (roomId: string | null) => {
    setSelectedRoomId(roomId);
    setError(null);
    if (!roomId) {
      setRoom(null); setEvents([]); setCursor(null); setDriverStatus({});
      return;
    }
    setLoading(true);
    try { await loadRoom(roomId, true); } catch (cause) { if (mountedRef.current) setError(toGroupRoomError(cause)); }
    finally { if (mountedRef.current) setLoading(false); }
  }, [loadRoom]);

  const refresh = useCallback(async () => {
    setRefreshing(true); setError(null);
    try {
      const listed = await loadRooms();
      const target = selectedRoomId ?? options.initialRoomId ?? listed[0]?.id ?? null;
      if (target && target !== selectedRoomId) setSelectedRoomId(target);
      if (target) await loadRoom(target, target !== selectedRoomId);
    } catch (cause) { if (mountedRef.current) setError(toGroupRoomError(cause)); }
    finally { if (mountedRef.current) { setRefreshing(false); setLoading(false); } }
  }, [loadRooms, loadRoom, options.initialRoomId, selectedRoomId]);

  const retry = useCallback(async () => { await refresh(); }, [refresh]);

  const send = useCallback(async (text: string, threadId?: string) => {
    if (!selectedRoomId || !text.trim()) return null;
    const eventId = `mc-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const optimistic: GroupEvent = {
      id: eventId,
      seq: (room?.latestSeq ?? events.at(-1)?.seq ?? 0) + 1,
      kind: 'message.user',
      actor: { kind: 'user', id: 'local' },
      message: { text, threadId: threadId ?? null, member: null },
      createdAt: Date.now(),
    };
    setEvents((current) => mergeGroupEvents(current, [optimistic]));
    try {
      const result = await clientRef.current.send(selectedRoomId, eventId, { text, ...(threadId ? { threadId } : {}) });
      setEvents((current) => mergeGroupEvents(current.filter((item) => item.id !== eventId && item.seq !== optimistic.seq), [result]));
      // Refresh the driver status immediately so the UI shows the members
      // as working right after a send, instead of waiting for the next poll.
      if (mountedRef.current) void loadRoom(selectedRoomId);
      return result;
    } catch (cause) { if (mountedRef.current) { setEvents((current) => current.filter((item) => item.id !== eventId)); setError(toGroupRoomError(cause)); } return null; }
  }, [events, loadRoom, room, selectedRoomId]);

  const disband = useCallback(async () => {
    if (!selectedRoomId) return;
    try { await clientRef.current.disband(selectedRoomId, `mc-${Date.now()}`); await loadRoom(selectedRoomId); }
    catch (cause) { if (mountedRef.current) setError(toGroupRoomError(cause)); }
  }, [loadRoom, selectedRoomId]);

  const approve = useCallback(async (params: { memberId?: string | null; taskId?: string | null; executionGeneration?: number; choice?: string | null; requestId?: string | null }) => {
    if (!selectedRoomId) throw new Error('No room selected.');
    const result = await clientRef.current.approve(selectedRoomId, params);
    if (mountedRef.current) { setError(null); await loadRoom(selectedRoomId); }
    return result;
  }, [loadRoom, selectedRoomId]);

  const retryMember = useCallback(async (taskId?: string | null) => {
    if (!selectedRoomId) throw new Error('No room selected.');
    const result = await clientRef.current.retry(selectedRoomId, taskId);
    if (mountedRef.current) { setError(null); await loadRoom(selectedRoomId); }
    return result;
  }, [loadRoom, selectedRoomId]);

  const stopRoom = useCallback(async () => {
    if (!selectedRoomId) return null;
    const result = await clientRef.current.stop(selectedRoomId);
    if (mountedRef.current) { setError(null); await loadRoom(selectedRoomId); }
    return result;
  }, [loadRoom, selectedRoomId]);

  const renameRoom = useCallback(async (name: string) => {
    if (!selectedRoomId || !name.trim()) return;
    try {
      await clientRef.current.rename(selectedRoomId, `mc-${Date.now()}`, name.trim());
      if (mountedRef.current) { setError(null); await loadRooms(); await loadRoom(selectedRoomId); }
    } catch (cause) { if (mountedRef.current) setError(toGroupRoomError(cause)); }
  }, [loadRoom, loadRooms, selectedRoomId]);

  useEffect(() => {
    mountedRef.current = true;
    if (options.enabled === false) { setLoading(false); return () => { mountedRef.current = false; }; }
    void (async () => {
      try {
        const [caps, listed] = await Promise.all([clientRef.current.capabilities(), loadRooms()]);
        if (!mountedRef.current) return;
        setCapabilities(caps);
        const target = selectedRoomId ?? options.initialRoomId ?? listed[0]?.id ?? null;
        if (target) await selectRoom(target);
      } catch (cause) { if (mountedRef.current) setError(toGroupRoomError(cause)); }
      finally { if (mountedRef.current) setLoading(false); }
    })();
    return () => { mountedRef.current = false; };
  }, [loadRooms, options.enabled]);

  useEffect(() => {
    if (!selectedRoomId || options.enabled === false || options.pollMs === 0) return undefined;
    const timer = window.setInterval(() => { void loadRoom(selectedRoomId); }, options.pollMs ?? 5000);
    return () => window.clearInterval(timer);
  }, [loadRoom, options.enabled, options.pollMs, selectedRoomId]);

  const status = statusParts(driverStatus);
  const memberActivity: Record<string, GroupEvent> = {};
  for (const event of events) if (event.actor.kind === 'member') memberActivity[event.actor.id] = event;
  const latestMemberEvent = events.filter((event) => event.actor.kind === 'member').at(-1);
  return {
    capabilities, driverAvailable: capabilities?.driver === true, rooms, room, selectedRoomId, events, cursor, loading, refreshing,
    error, serviceUnavailable: error?.retryable === true && /unavailable|connect|closed|timed out/i.test(error.message), authorityChanged,
    driverStatus, pendingActions: status.pendingActions, approval: status.approval, blocked: status.blocked,
    working: driverStatus.working === true || status.pendingActions.length > 0,
    disbanded: Boolean(room?.disbandedAt), memberActivity, round: deriveRoundCoordinates(events),
    focusHandle: latestMemberEvent?.message.member?.handle ?? null,
    selectRoom, refresh, retry, send, disband, clearAuthorityChange: () => setAuthorityChanged(false),
    approve, retryMember, renameRoom,
    stopRoom,
  };
}
