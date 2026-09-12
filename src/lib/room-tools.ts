/**
 * Room tool-trace fetch helpers.
 *
 * The dashboard-api proxy (`/api/local`) forwards to the Mission Control
 * telemetry server (:8765), which exposes tool traces collected from the
 * member profiles' Group: sessions. The UI renders them with the same
 * ToolMessage component used by the normal chat.
 */
import { localApiUrl, buildHeaders, MissionControlAuthError } from './hermes-api';

export type RoomToolTrace = {
  toolName: string;
  toolInput?: string;
  output?: string;
  status?: 'complete' | 'streaming' | 'error';
  durationS?: number | null;
  timestamp?: number;
  memberHandle?: string;
  memberProfile?: string;
  memberDisplayName?: string;
};

export type RoomToolsResponse = {
  room_id?: string;
  tools: RoomToolTrace[];
};

export async function loadRoomTools(roomId: string, accessToken?: string, maxAge = 5): Promise<RoomToolTrace[]> {
  if (!roomId) return [];
  const query = `room_id=${encodeURIComponent(roomId)}&max_age=${maxAge}`;
  const response = await fetch(localApiUrl(`/room/tools?${query}`), {
    headers: buildHeaders(accessToken),
    cache: 'no-store',
  });
  if (response.status === 401) throw new MissionControlAuthError();
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = payload?.detail || payload?.error || '';
    throw new Error(`Room tools failed (${response.status})${detail ? `: ${detail}` : ''}`);
  }
  const data = (await response.json()) as RoomToolsResponse;
  return Array.isArray(data.tools) ? data.tools : [];
}
