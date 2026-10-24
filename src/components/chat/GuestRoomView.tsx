import { useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, Bot, Check, Lock, LogOut, ShieldCheck, X } from 'lucide-react';
import { useI18n } from '../../lib/i18n';
import {
  createRoomGuestApi, mergeGuestEvents,
  type GuestBot, type GuestLogEvent, type GuestRoom, type RoomGuestApi,
} from '../../lib/room-guest-api';
import { defaultGuestTab, guestEventToChatMessage, guestOfflineTime } from '../../lib/room-guest-view-model';
import { inviteErrorCode, isInviteUnavailable } from '../../lib/invite-ui-errors';
import { Button } from '../ui/Button';
import { SegmentedControl } from '../ui/SegmentedControl';
import { RoomTranscriptRow } from './RoomTranscriptRow';

type GuestRoomViewProps = { room: GuestRoom; accessToken?: string; api?: RoomGuestApi; onLeave?: () => void };

type GuestBotPanelProps = {
  room: GuestRoom;
  bot: GuestBot | null;
  busy: boolean;
  onDecide: (id: string, choice: 'approve' | 'reject') => void;
  onLeave: () => void;
};

function GuestBotPanel({ room, bot, busy, onDecide, onLeave }: GuestBotPanelProps) {
  const { t } = useI18n();
  const status = bot?.status === 'online' ? 'online' : bot?.status === 'pending' ? 'pending' : 'offline';
  return (
    <div className="guest-bot-panel">
      <header className="flex items-center gap-3">
        <div className="guest-bot-avatar"><Bot size={24} aria-hidden="true" /></div>
        <div>
          <h3 className="text-lg font-semibold text-text">{t('inviteUi.myBot')}</h3>
          <p className="mt-1 flex items-center gap-2 text-xs text-text-muted" role="status">
            <span className={`guest-status-dot is-${status}`} aria-hidden="true" />
            {t(bot ? `inviteUi.botStatus.${status}` : 'inviteUi.loading')}
          </p>
        </div>
      </header>
      <section className="guest-panel-section">
        <h4>{t('inviteUi.approvals')}</h4>
        <div className="mt-3 grid gap-3">
          {bot?.approvals.length ? bot.approvals.map((request) => (
            <article className="guest-approval" key={request.request_id}>
              <p className="font-semibold text-text">{request.title}</p>
              <p className="mt-2 text-xs leading-relaxed text-text-muted">{request.detail}</p>
              <div className="mt-4 grid grid-cols-2 gap-3">
                <Button size="sm" variant="primary" disabled={busy} icon={<Check size={15} />}
                  onClick={() => onDecide(request.request_id, 'approve')}>
                  {t('inviteUi.approve')}
                </Button>
                <Button size="sm" variant="danger" disabled={busy} icon={<X size={15} />}
                  onClick={() => onDecide(request.request_id, 'reject')}>
                  {t('inviteUi.reject')}
                </Button>
              </div>
            </article>
          )) : <p className="text-sm text-text-muted">{t(bot ? 'inviteUi.noApprovals' : 'inviteUi.loading')}</p>}
        </div>
      </section>
      <section className="guest-panel-section">
        <h4>{t('inviteUi.permissions')}</h4>
        <ul className="mt-3 grid gap-2 text-sm text-text-muted">
          {(bot?.permissions ?? room.permissions).map((permission) => (
            <li className="flex items-start gap-2 break-words" key={permission}>
              <ShieldCheck size={16} className="mt-0.5 shrink-0 text-accent" aria-hidden="true" />
              <span className="min-w-0">{permission}</span>
            </li>
          ))}
        </ul>
      </section>
      <section className="guest-panel-section">
        <h4>{t('inviteUi.members')}</h4>
        <ul className="mt-3 grid gap-2 text-sm text-text">
          {room.members.map((member) => <li className="break-words" key={member.id}>{member.name}</li>)}
        </ul>
        <p className="mt-3 text-xs leading-relaxed text-text-muted">{t('inviteUi.namesHint')}</p>
      </section>
      <Button className="mt-6 w-full" variant="danger" disabled={busy} icon={<LogOut size={15} />} onClick={onLeave}>
        {t('inviteUi.leave')}
      </Button>
    </div>
  );
}

function GuestTranscript({ events, hasMore, busy, onLoadMore, owner }: {
  events: GuestLogEvent[];
  hasMore: boolean;
  busy: boolean;
  onLoadMore: () => void;
  owner: string;
}) {
  const { t } = useI18n();
  return (
    <section className="guest-transcript" aria-label={t('inviteUi.chat')}>
      <div className="invite-notice mb-5" role="note">
        <Lock size={16} className="shrink-0" aria-hidden="true" />
        <p>{t('inviteUi.readOnly', { owner })}</p>
      </div>
      <div className="grid gap-4">
        {events.map((event) => (
          <RoomTranscriptRow key={event.event_id} eventId={event.event_id} sequence={event.seq}
            message={guestEventToChatMessage(event)} />
        ))}
        {!events.length ? <p className="text-sm text-text-muted">{t('inviteUi.noMessages')}</p> : null}
        {hasMore ? (
          <Button loading={busy} onClick={onLoadMore}>{t('inviteUi.loadMore')}</Button>
        ) : null}
      </div>
    </section>
  );
}

export function GuestRoomView({ room, accessToken, api: apiOverride, onLeave }: GuestRoomViewProps) {
  const { t } = useI18n();
  const api = useMemo(() => apiOverride ?? createRoomGuestApi(accessToken), [accessToken, apiOverride]);
  const [tab, setTab] = useState<'bot' | 'chat'>(defaultGuestTab);
  const [bot, setBot] = useState<GuestBot | null>(null);
  const [events, setEvents] = useState<GuestLogEvent[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hidden, setHidden] = useState(false);
  const [busy, setBusy] = useState(false);
  const lifecycle = useRef<AbortController | null>(null);
  const sequence = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    lifecycle.current = controller;
    setEvents([]);
    setBot(null);
    setError(null);
    setHidden(false);
    setTab(defaultGuestTab);
    sequence.current = 0;
    Promise.all([
      api.getMyBot(room.room_id, controller.signal),
      api.getLog(room.room_id, { sinceSeq: 0, limit: 100 }, controller.signal),
    ]).then(([nextBot, page]) => {
      if (controller.signal.aborted) return;
      setBot(nextBot);
      setEvents(mergeGuestEvents([], page.events));
      sequence.current = Math.max(0, ...page.events.map((event) => event.seq));
      setHasMore(page.has_more);
    }).catch((cause) => {
      if (controller.signal.aborted) return;
      if (isInviteUnavailable(cause)) setHidden(true);
      else setError(inviteErrorCode(cause));
    });
    return () => controller.abort();
  }, [api, room.room_id]);

  const run = async (action: (signal: AbortSignal) => Promise<void>) => {
    const signal = lifecycle.current?.signal;
    if (busy || !signal || signal.aborted) return;
    setBusy(true);
    setError(null);
    try { await action(signal); }
    catch (cause) {
      if (!signal.aborted) {
        if (isInviteUnavailable(cause)) setHidden(true);
        else setError(inviteErrorCode(cause));
      }
    }
    finally { if (!signal.aborted) setBusy(false); }
  };
  const loadMore = () => run(async (signal) => {
    const page = await api.getLog(room.room_id, { sinceSeq: sequence.current, limit: 100 }, signal);
    if (signal.aborted) return;
    setEvents((current) => mergeGuestEvents(current, page.events));
    const next = Math.max(sequence.current, ...page.events.map((event) => event.seq));
    setHasMore(page.has_more && next > sequence.current);
    sequence.current = next;
  });
  const decide = (id: string, choice: 'approve' | 'reject') => run(async (signal) => {
    await api.decideApproval(room.room_id, id, choice, signal);
    if (!signal.aborted) setBot((current) => current ? {
      ...current, approvals: current.approvals.filter((request) => request.request_id !== id),
    } : null);
  });
  const leave = () => run(async (signal) => {
    await api.leaveRoom(room.room_id, signal);
    if (!signal.aborted) onLeave?.();
  });

  if (hidden) return null;
  return (
    <section className="invite-ui guest-room-view" aria-label={room.name}>
      <header className="guest-room-header">
        <div className="flex min-w-0 items-start gap-3">
          <Lock size={18} className="mt-1 shrink-0 text-accent" aria-hidden="true" />
          <div className="min-w-0">
            <h2 className="break-words text-base font-semibold text-text">{room.name}</h2>
            <p className="mt-1 text-xs text-text-muted">{t('inviteUi.guest')} · {room.owner}</p>
          </div>
        </div>
        <div className="guest-mobile-tabs">
          <SegmentedControl value={tab} label={t('inviteUi.guestView')}
            options={[{ value: 'bot', label: t('inviteUi.myBot') }, { value: 'chat', label: t('inviteUi.chat') }]}
            onChange={setTab} />
        </div>
      </header>
      {!room.owner_online ? (
        <div className="invite-error guest-offline" role="alert">
          <AlertTriangle size={19} className="mt-0.5 shrink-0" aria-hidden="true" />
          <p>{t('inviteUi.offline', { owner: room.owner, time: guestOfflineTime(room.owner_offline_since) })}</p>
        </div>
      ) : null}
      {error ? <p className="invite-error m-4" role="alert">{t('inviteUi.error')} ({error})</p> : null}
      <div className="guest-room-layout" data-active-tab={tab}>
        <aside className="guest-bot-column" aria-label={t('inviteUi.myBot')}>
          <GuestBotPanel room={room} bot={bot} busy={busy} onDecide={(id, choice) => void decide(id, choice)}
            onLeave={() => void leave()} />
        </aside>
        <div className="guest-chat-column">
          <GuestTranscript events={events} hasMore={hasMore} busy={busy} onLoadMore={() => void loadMore()} owner={room.owner} />
        </div>
      </div>
    </section>
  );
}
