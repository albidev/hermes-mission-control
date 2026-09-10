import type { GroupEvent, GroupMember } from '../../lib/group-gateway.ts';

export type GroupMemberStatus = 'idle' | 'working' | 'settled' | 'unavailable';
export type VisibleGroupEvent = { event: GroupEvent; member: GroupMember | null };

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function statusValue(value: unknown): GroupMemberStatus | null {
  if (typeof value !== 'string') return null;
  const normalized = value.toLowerCase();
  if (normalized.includes('unavail') || normalized.includes('offline') || normalized.includes('error')) return 'unavailable';
  if (normalized.includes('work') || normalized.includes('run') || normalized.includes('active') || normalized.includes('pending')) return 'working';
  if (normalized.includes('sett') || normalized.includes('done') || normalized.includes('complete') || normalized.includes('finish')) return 'settled';
  if (normalized.includes('idle') || normalized.includes('ready')) return 'idle';
  return null;
}

export function deriveGroupMemberStatus(member: GroupMember, driverStatus: Record<string, unknown>, latestEvent: GroupEvent | null): GroupMemberStatus {
  const unavailable = driverStatus.unavailable;
  if (Array.isArray(unavailable) && unavailable.some((item) => item === member.id || item === member.handle)) return 'unavailable';
  const members = record(driverStatus.members);
  const memberStatus = record(members[member.id] ?? members[member.handle]);
  const direct = statusValue(memberStatus.status) ?? statusValue(memberStatus.state);
  if (direct) return direct;
  const targetedId = driverStatus.member_id ?? driverStatus.memberId ?? driverStatus.active_member_id;
  const global = statusValue(driverStatus.status) ?? statusValue(driverStatus.state);
  if (global && (!targetedId || targetedId === member.id || targetedId === member.handle)) return global;
  if (latestEvent?.actor.kind === 'member' && (latestEvent.actor.id === member.id || latestEvent.message.member?.id === member.id)) return 'settled';
  return 'idle';
}

export function visibleGroupEvents(events: GroupEvent[], members: GroupMember[] = []): VisibleGroupEvent[] {
  return events.flatMap((event) => {
    const isUser = event.actor.kind === 'user' || event.kind === 'message.user';
    const isFinalMember = event.actor.kind === 'member' && (event.kind === 'message.member' || Boolean(event.message.member));
    if (!isUser && !isFinalMember) return [];
    const memberId = event.message.member?.id ?? (event.actor.kind === 'member' ? event.actor.id : null);
    return [{ event, member: memberId ? members.find((item) => item.id === memberId || item.handle === memberId) ?? null : null }];
  });
}

export function groupEventLabel(event: GroupEvent): string {
  if (event.kind === 'message.user' || event.actor.kind === 'user') return 'You';
  if (event.kind === 'message.member' || event.message.member) return 'Final answer';
  const label = event.kind.replace(/[._-]+/g, ' ');
  return label ? `${label[0].toUpperCase()}${label.slice(1)}` : 'Activity';
}
