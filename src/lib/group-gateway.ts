import { requestBotRpc, BotRpcError } from './bot-gateway';

export const GROUPS_PROTOCOL_VERSION = 2;

export type GroupActorKind = 'user' | 'member' | 'gateway' | 'system';
export type GroupActor = { kind: GroupActorKind; id: string; displayName?: string };
export type GroupMember = {
  id: string;
  profile: string;
  handle: string;
  displayName?: string;
  targetInstallId?: string;
  targetProfile?: string;
};
export type GroupRoom = {
  id: string;
  name: string;
  authorityGatewayId: string;
  authorityEpoch: number;
  latestSeq: number;
  disbandedAt: string | number | null;
  members: GroupMember[];
};
export type GroupMessage = { text: string; threadId: string | null; member: { id: string; handle?: string; displayName?: string } | null };
export type GroupEvent = {
  id: string;
  seq: number;
  kind: string;
  actor: GroupActor;
  message: GroupMessage;
  createdAt: string | number | null;
  round?: number;
  coordinates?: { x: number; y: number };
};
export type GroupAuthority = { gatewayId: string; epoch: number };
export type GroupLogPage = { events: GroupEvent[]; cursor: number | string | null; latestSeq: number; hasMore: boolean; authority: GroupAuthority | null };
export type GroupCapabilities = { protocolVersion: number; driver: boolean; methods: string[]; maxLogLimit: number; authorityGatewayId: string };
export type GroupState = { room: GroupRoom; driverStatus?: Record<string, unknown> };

export type GroupMemberWire = { member_id?: unknown; id?: unknown; profile?: unknown; handle?: unknown; display_name?: unknown; target_install_id?: unknown; target_profile?: unknown };
export type GroupRoomWire = { room_id?: unknown; id?: unknown; name?: unknown; authority_gateway_id?: unknown; authority_epoch?: unknown; latest_seq?: unknown; disbanded_at?: unknown; members?: unknown[] };
export type GroupEventWire = { event_id?: unknown; id?: unknown; seq?: unknown; kind?: unknown; actor?: unknown; payload?: unknown; created_at?: unknown };
export type GroupLogWire = { events?: unknown[]; cursor?: unknown; latest_seq?: unknown; has_more?: unknown; authority?: unknown };
export type GroupCapabilitiesWire = { protocol_version?: unknown; driver?: unknown; methods?: unknown[]; max_log_limit?: unknown; authority_gateway_id?: unknown };
export type GroupStateWire = { room?: GroupRoomWire; driver_status?: unknown };
type WireRecord = Record<string, unknown>;
export type GroupRpc = <T = unknown>(method: string, params: Record<string, unknown>) => Promise<T>;

function record(value: unknown): WireRecord { return value && typeof value === 'object' && !Array.isArray(value) ? value as WireRecord : {}; }
function stringValue(value: unknown, fallback = ''): string { return typeof value === 'string' ? value : fallback; }
function numberValue(value: unknown, fallback = 0): number { return typeof value === 'number' && Number.isFinite(value) ? value : fallback; }
function optionalString(value: unknown): string | undefined { return typeof value === 'string' && value.trim() ? value : undefined; }

export function normalizeGroupMember(value: unknown): GroupMember {
  const item = record(value);
  return {
    id: stringValue(item.member_id ?? item.id),
    profile: stringValue(item.profile),
    handle: stringValue(item.handle),
    ...(optionalString(item.display_name) ? { displayName: item.display_name as string } : {}),
    ...(optionalString(item.target_install_id) ? { targetInstallId: item.target_install_id as string } : {}),
    ...(optionalString(item.target_profile) ? { targetProfile: item.target_profile as string } : {}),
  };
}

export function normalizeGroupRoom(value: unknown): GroupRoom {
  const item = record(value);
  return {
    id: stringValue(item.room_id ?? item.id), name: stringValue(item.name),
    authorityGatewayId: stringValue(item.authority_gateway_id), authorityEpoch: numberValue(item.authority_epoch),
    latestSeq: numberValue(item.latest_seq), disbandedAt: (typeof item.disbanded_at === 'string' || typeof item.disbanded_at === 'number') ? item.disbanded_at : null,
    members: Array.isArray(item.members) ? item.members.map(normalizeGroupMember) : [],
  };
}

export function normalizeGroupEvent(value: unknown): GroupEvent {
  const item = record(value); const actor = record(item.actor); const payload = record(item.payload); const member = record(payload.member);
  const hasMember = Object.keys(member).length > 0;
  const round = numberValue(item.round ?? payload.round, -1);
  const coordinates = record(item.coordinates ?? payload.coordinates);
  const x = numberValue(coordinates.x, -1);
  const y = numberValue(coordinates.y, -1);
  return {
    id: stringValue(item.event_id ?? item.id), seq: numberValue(item.seq), kind: stringValue(item.kind),
    actor: { kind: (['user', 'member', 'gateway', 'system'].includes(actor.kind as string) ? actor.kind : 'system') as GroupActorKind, id: stringValue(actor.id) },
    message: { text: stringValue(payload.text), threadId: optionalString(payload.thread_id) ?? null, member: hasMember ? { id: stringValue(member.member_id ?? member.id), ...(optionalString(member.handle) ? { handle: member.handle as string } : {}), ...(optionalString(member.display_name) ? { displayName: member.display_name as string } : {}) } : null },
    createdAt: (typeof item.created_at === 'string' || typeof item.created_at === 'number') ? item.created_at : null,
    ...(round >= 0 ? { round } : {}),
    ...(x >= 0 && y >= 0 ? { coordinates: { x, y } } : {}),
  };
}

export function normalizeGroupLog(value: unknown): GroupLogPage {
  const item = record(value); const authority = record(item.authority);
  return { events: Array.isArray(item.events) ? item.events.map(normalizeGroupEvent) : [], cursor: (typeof item.cursor === 'string' || typeof item.cursor === 'number') ? item.cursor : null, latestSeq: numberValue(item.latest_seq), hasMore: item.has_more === true, authority: Object.keys(authority).length ? { gatewayId: stringValue(authority.gateway_id), epoch: numberValue(authority.epoch) } : null };
}

export function normalizeGroupCapabilities(value: unknown): GroupCapabilities {
  const item = record(value);
  return { protocolVersion: numberValue(item.protocol_version), driver: item.driver === true, methods: Array.isArray(item.methods) ? item.methods.filter((method): method is string => typeof method === 'string') : [], maxLogLimit: numberValue(item.max_log_limit), authorityGatewayId: stringValue(item.authority_gateway_id) };
}

export class GroupServiceUnavailableError extends Error {
  constructor(message = 'Group Chat worker is unavailable. Restart the Hermes gateway and try again.') { super(message); this.name = 'GroupServiceUnavailableError'; }
}

export class GroupValidationError extends Error {
  constructor(message: string) { super(message); this.name = 'GroupValidationError'; }
}
export { BotRpcError as GroupRpcError };

export type GroupCreateInput = { roomId: string; name: string; roster: GroupMember[] };
export type GroupListOptions = { limit?: number; offset?: number; cursor?: string | number; includeDisbanded?: boolean };

async function defaultGroupRpc<T>(method: string, params: Record<string, unknown>): Promise<T> {
  try {
    return await requestBotRpc<T>(method, params);
  } catch (error) {
    if (error instanceof Error && /connect|connection closed|timed out/i.test(error.message)) {
      throw new GroupServiceUnavailableError(error.message);
    }
    throw error;
  }
}

export class GroupGatewayClient {
  private readonly rpc: GroupRpc;

  constructor(rpc: GroupRpc = defaultGroupRpc) {
    this.rpc = rpc;
  }

  async capabilities(): Promise<GroupCapabilities> { return normalizeGroupCapabilities(await this.rpc('groups.capabilities', {})); }

  async list(options: GroupListOptions = {}): Promise<{ rooms: GroupRoom[]; nextOffset: number | null }> {
    const result = record(await this.rpc('groups.list', { ...(options.limit === undefined ? {} : { limit: options.limit }), ...(options.offset === undefined ? {} : { offset: options.offset }), ...(options.cursor === undefined ? {} : { cursor: options.cursor }), ...(options.includeDisbanded ? { include_disbanded: true } : {}) }));
    return { rooms: Array.isArray(result.rooms) ? result.rooms.map(normalizeGroupRoom) : [], nextOffset: typeof result.next_offset === 'number' ? result.next_offset : null };
  }

  async create(input: GroupCreateInput): Promise<GroupRoom> {
    if (input.roster.length < 2 || input.roster.length > 6) {
      throw new GroupValidationError('A Group Chat roster must contain between 2 and 6 members.');
    }
    const members = input.roster.map((member) => ({
      member_id: member.id,
      profile: member.profile,
      handle: member.handle,
      ...(member.displayName ? { display_name: member.displayName } : {}),
      ...(member.targetInstallId ? { target_install_id: member.targetInstallId } : {}),
      ...(member.targetProfile ? { target_profile: member.targetProfile } : {}),
    }));
    const result = record(await this.rpc('groups.create', { room_id: input.roomId, name: input.name, members }));
    return normalizeGroupRoom(result.room);
  }

  async state(roomId: string, includeDisbanded = false): Promise<GroupState> {
    const result = record(await this.rpc('groups.state', { room_id: roomId, ...(includeDisbanded ? { include_disbanded: true } : {}) }));
    const driverStatus = record(result.driver_status);
    return { room: normalizeGroupRoom(result.room), ...(Object.keys(driverStatus).length ? { driverStatus } : {}) };
  }

  async send(roomId: string, eventId: string, message: { text: string; threadId?: string }): Promise<GroupEvent> {
    const result = record(await this.rpc('groups.send', { room_id: roomId, event_id: eventId, payload: { text: message.text, ...(message.threadId ? { thread_id: message.threadId } : {}) } }));
    return normalizeGroupEvent(result.event);
  }

  async rename(roomId: string, eventId: string, name: string): Promise<GroupRoom> {
    const result = record(await this.rpc('groups.rename', { room_id: roomId, event_id: eventId, name }));
    return normalizeGroupRoom(result.room);
  }

  async log(roomId: string, options: { sinceSeq?: number; cursor?: string | number; limit?: number; includeDisbanded?: boolean } = {}): Promise<GroupLogPage> {
    return normalizeGroupLog(await this.rpc('groups.log', { room_id: roomId, ...(options.sinceSeq === undefined ? {} : { since_seq: options.sinceSeq }), ...(options.cursor === undefined ? {} : { cursor: options.cursor }), ...(options.limit === undefined ? {} : { limit: options.limit }), ...(options.includeDisbanded ? { include_disbanded: true } : {}) }));
  }

  async disband(roomId: string, eventId?: string): Promise<unknown> {
    return this.rpc('groups.disband', { room_id: roomId, ...(eventId ? { event_id: eventId } : {}) });
  }
}

export { BotRpcError };