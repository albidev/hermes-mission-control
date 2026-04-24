# Mission Control Agents Upgrade-Safe Contract Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Replace the current fake/fallback Agents data path with an upgrade-safe contract that keeps the Agents section alive from filesystem-backed session artifacts and degrades gracefully when deep trace internals are unavailable.

**Architecture:** The backend on `hermes_cli/web_server.py` (port `9119`) becomes the adapter boundary. It exposes a stable Mission Control contract for agents, sessions, and per-session trace. Under the hood it reads from three tiers in order: (1) durable session artifacts in `~/.hermes/sessions`, (2) `SessionDB`/`hermes_state.py` when available, and (3) explicit degraded/unavailable payloads instead of 500s.

**Tech Stack:** FastAPI, Pydantic, existing Hermes dashboard backend (`hermes_cli/web_server.py`), Mission Control React/Vite frontend, existing `hermes-api.ts` normalization layer, filesystem session artifacts, optional `SessionDB` SQLite access.

---

## Why this plan exists

Current state in the codebase:

- `apps/mission-control/src/lib/hermes-api.ts:1423-1430` returns `fallbackAgentTrace` unconditionally.
- `apps/mission-control/src/routes/AgentsRoute.tsx` already has serious UI for timeline/DAG/live selection, but it is fed by fake data paths and derived session aggregates.
- `hermes_cli/web_server.py:718-737` already exposes `/api/sessions` from `SessionDB.list_sessions_rich`, which is useful but not sufficient as the stable contract for Mission Control Agents.
- `hermes_state.py:793-904` gives a reasonably stable rich session list.
- `hermes_state.py:945-1040` and `1022-1040` expose message storage/loading, but these are internal interfaces and must be wrapped behind a Mission Control-owned adapter.
- `~/.hermes/sessions/sessions.json` and `session_<id>.json` are the most durable, upgrade-resistant source for live session discovery.

The mistake to avoid is letting the frontend depend directly on core message/storage internals. That way lies update pain and bad life choices.

---

## Contract rules (non-negotiable)

1. **Frontend never reads core internals directly.** It only reads Mission Control API payloads.
2. **Every Agents endpoint always returns the same shape.** Fail soft, not loud.
3. **Trace declares its fidelity.** `traceMode` is required and one of:
   - `native`
   - `transcript`
   - `unavailable`
4. **Filesystem-backed session discovery is the default base.**
5. **`SessionDB` is a best-effort enhancement, not a hard dependency.**
6. **DAG is optional. Timeline is mandatory.**
7. **No 500 just because native trace failed.** Return degraded payload with warnings.

---

## Stable API contract

### `GET /api/mission-control/agents`

Purpose: aggregated registry of agents grouped by `source::model`.

#### Response shape

```json
{
  "success": true,
  "schemaVersion": "1",
  "available": true,
  "capabilities": {
    "trace": true,
    "traceModes": ["native", "transcript", "unavailable"]
  },
  "items": [
    {
      "agentId": "discord::gpt-5.4",
      "source": "discord",
      "model": "gpt-5.4",
      "label": "discord / gpt-5.4",
      "totalSessions": 12,
      "liveSessions": 2,
      "lastActiveAt": "2026-04-24T10:39:21Z",
      "traceMode": "transcript"
    }
  ]
}
```

### `GET /api/mission-control/sessions`

Purpose: normalized session list for Agents and Sessions UI.

#### Response shape

```json
{
  "success": true,
  "schemaVersion": "1",
  "available": true,
  "items": [
    {
      "sessionId": "20260424_094245_a263f3",
      "agentId": "tui::gpt-5.4",
      "title": "Mission Control agent section discussion",
      "source": "tui",
      "platform": "tui",
      "chatType": "local",
      "displayName": "TUI local session",
      "model": "gpt-5.4",
      "startedAt": "2026-04-24T09:42:46Z",
      "lastActiveAt": "2026-04-24T10:39:21Z",
      "status": "live",
      "messageCount": 31,
      "traceMode": "transcript",
      "preview": "per mission control la sezione agent..."
    }
  ],
  "stats": {
    "totalSessions": 48,
    "liveSessions": 3,
    "activeAgents": 2
  }
}
```

### `GET /api/mission-control/agents/trace?session_id=...`

Purpose: per-session trace with fidelity declaration.

#### Response shape

```json
{
  "success": true,
  "schemaVersion": "1",
  "available": true,
  "mode": "post",
  "traceMode": "transcript",
  "session": {
    "sessionId": "20260424_094245_a263f3",
    "agentId": "tui::gpt-5.4",
    "model": "gpt-5.4",
    "source": "tui",
    "startedAt": "2026-04-24T09:42:46Z",
    "lastActiveAt": "2026-04-24T10:39:21Z"
  },
  "events": [
    {
      "id": "evt_1",
      "type": "user_message",
      "label": "User message",
      "detail": "per mission control...",
      "tone": "good",
      "status": "completed",
      "timestamp": "2026-04-24T10:39:00Z",
      "sessionId": "20260424_094245_a263f3",
      "turnId": 1
    }
  ],
  "nodes": [],
  "edges": [],
  "stats": {
    "turns": 1,
    "toolCalls": 0,
    "skills": 0,
    "thoughts": 0,
    "errors": 0,
    "durationSeconds": 10
  },
  "warnings": [
    "Native tool-call trace unavailable; built from transcript only."
  ]
}
```

---

## TypeScript schema to add

Create a Mission Control-owned contract instead of overloading the current generic session item shape.

### File: `apps/mission-control/src/lib/hermes-api.ts`

Add these types near the existing Agents/session types.

```ts
export type MissionControlTraceMode = 'native' | 'transcript' | 'unavailable';
export type MissionControlSessionStatus = 'live' | 'idle' | 'ended';

export type MissionControlAgentRegistryItem = {
  agentId: string;
  source: string;
  model: string;
  label: string;
  totalSessions: number;
  liveSessions: number;
  lastActiveAt: string | null;
  traceMode: MissionControlTraceMode;
};

export type MissionControlAgentsSnapshot = {
  success: boolean;
  schemaVersion: string;
  available: boolean;
  capabilities: {
    trace: boolean;
    traceModes: MissionControlTraceMode[];
  };
  items: MissionControlAgentRegistryItem[];
};

export type MissionControlAgentSessionItem = {
  sessionId: string;
  agentId: string;
  title: string;
  source: string;
  platform: string;
  chatType: string;
  displayName: string;
  model: string;
  startedAt: string | null;
  lastActiveAt: string | null;
  endedAt?: string | null;
  status: MissionControlSessionStatus;
  messageCount: number;
  traceMode: MissionControlTraceMode;
  preview: string;
};

export type MissionControlAgentsSessionsSnapshot = {
  success: boolean;
  schemaVersion: string;
  available: boolean;
  items: MissionControlAgentSessionItem[];
  stats: {
    totalSessions: number;
    liveSessions: number;
    activeAgents: number;
  };
};

export type MissionControlAgentTraceEvent = {
  id: string;
  type: string;
  label: string;
  detail: string;
  tone: 'good' | 'warn' | 'bad';
  status?: string;
  timestamp: string;
  sessionId: string;
  turnId: number;
  parentEventId?: string;
  toolName?: string;
  callId?: string;
  skillName?: string;
  request?: string;
  response?: string;
};

export type MissionControlAgentTraceNode = {
  id: string;
  kind: string;
  label: string;
  status: string;
  turnId: number;
  timestamp: string;
};

export type MissionControlAgentTraceEdge = {
  from: string;
  to: string;
  kind: string;
};

export type MissionControlAgentTraceSessionRef = {
  sessionId: string;
  agentId: string;
  model: string;
  source: string;
  startedAt: string | null;
  lastActiveAt: string | null;
};

export type MissionControlAgentTraceSnapshot = {
  success: boolean;
  schemaVersion: string;
  available: boolean;
  mode: 'live' | 'post';
  traceMode: MissionControlTraceMode;
  session: MissionControlAgentTraceSessionRef | null;
  events: MissionControlAgentTraceEvent[];
  nodes: MissionControlAgentTraceNode[];
  edges: MissionControlAgentTraceEdge[];
  stats: {
    turns: number;
    toolCalls: number;
    skills: number;
    thoughts: number;
    errors: number;
    durationSeconds: number;
  };
  warnings: string[];
};
```

### Normalization helpers to add

```ts
function getAgentKey(source?: string, model?: string): string {
  return `${source || 'unknown'}::${model || 'unknown'}`;
}
```

```ts
function normalizeTraceMode(value: unknown): MissionControlTraceMode {
  return value === 'native' || value === 'transcript' || value === 'unavailable'
    ? value
    : 'unavailable';
}
```

Do not reuse the current `MissionControlSessionItem` blindly. It is timestamp-number based and shaped around `/api/sessions`, not the upgrade-safe Agents contract.

---

## Python/Pydantic schema to add

### File: `hermes_cli/web_server.py`

Add these Pydantic models near the existing request/response models.

```python
from typing import Literal, Optional
from pydantic import BaseModel, Field

TraceMode = Literal["native", "transcript", "unavailable"]
SessionStatus = Literal["live", "idle", "ended"]
TraceViewMode = Literal["live", "post"]

class MissionControlAgentsCapabilities(BaseModel):
    trace: bool = True
    traceModes: list[TraceMode] = Field(default_factory=lambda: ["native", "transcript", "unavailable"])

class MissionControlAgentRegistryItem(BaseModel):
    agentId: str
    source: str
    model: str
    label: str
    totalSessions: int
    liveSessions: int
    lastActiveAt: Optional[str] = None
    traceMode: TraceMode

class MissionControlAgentsSnapshot(BaseModel):
    success: bool = True
    schemaVersion: str = "1"
    available: bool = True
    capabilities: MissionControlAgentsCapabilities
    items: list[MissionControlAgentRegistryItem]

class MissionControlAgentSessionItem(BaseModel):
    sessionId: str
    agentId: str
    title: str
    source: str
    platform: str
    chatType: str
    displayName: str
    model: str
    startedAt: Optional[str] = None
    lastActiveAt: Optional[str] = None
    endedAt: Optional[str] = None
    status: SessionStatus
    messageCount: int = 0
    traceMode: TraceMode
    preview: str = ""

class MissionControlAgentsSessionsStats(BaseModel):
    totalSessions: int
    liveSessions: int
    activeAgents: int

class MissionControlAgentsSessionsSnapshot(BaseModel):
    success: bool = True
    schemaVersion: str = "1"
    available: bool = True
    items: list[MissionControlAgentSessionItem]
    stats: MissionControlAgentsSessionsStats

class MissionControlTraceSessionRef(BaseModel):
    sessionId: str
    agentId: str
    model: str
    source: str
    startedAt: Optional[str] = None
    lastActiveAt: Optional[str] = None

class MissionControlTraceEvent(BaseModel):
    id: str
    type: str
    label: str
    detail: str
    tone: Literal["good", "warn", "bad"] = "good"
    status: Optional[str] = None
    timestamp: str
    sessionId: str
    turnId: int
    parentEventId: Optional[str] = None
    toolName: Optional[str] = None
    callId: Optional[str] = None
    skillName: Optional[str] = None
    request: Optional[str] = None
    response: Optional[str] = None

class MissionControlTraceNode(BaseModel):
    id: str
    kind: str
    label: str
    status: str
    turnId: int
    timestamp: str

class MissionControlTraceEdge(BaseModel):
    from_: str = Field(alias="from")
    to: str
    kind: str

class MissionControlTraceStats(BaseModel):
    turns: int = 0
    toolCalls: int = 0
    skills: int = 0
    thoughts: int = 0
    errors: int = 0
    durationSeconds: int = 0

class MissionControlAgentTraceSnapshot(BaseModel):
    success: bool = True
    schemaVersion: str = "1"
    available: bool = True
    mode: TraceViewMode = "post"
    traceMode: TraceMode
    session: Optional[MissionControlTraceSessionRef] = None
    events: list[MissionControlTraceEvent] = Field(default_factory=list)
    nodes: list[MissionControlTraceNode] = Field(default_factory=list)
    edges: list[MissionControlTraceEdge] = Field(default_factory=list)
    stats: MissionControlTraceStats = Field(default_factory=MissionControlTraceStats)
    warnings: list[str] = Field(default_factory=list)
```

Note: if you hate `from_` aliasing, good. Python hates `from` too. Use aliasing and move on.

---

## Backend provider split

Do **not** dump all logic into one 300-line endpoint block in `web_server.py`. That’s how software gets the smell.

### Create: `hermes_cli/mission_control_agents.py`

This module should own the adapter logic.

#### Public functions

```python
def load_agents_snapshot(live_window_seconds: int = 300) -> MissionControlAgentsSnapshot: ...
def load_agents_sessions_snapshot(limit: int = 100, live_window_seconds: int = 300) -> MissionControlAgentsSessionsSnapshot: ...
def load_agent_trace_snapshot(session_id: str, limit: int = 300, compact: bool = False) -> MissionControlAgentTraceSnapshot: ...
```

#### Internal helpers

```python
def _read_gateway_sessions_index() -> dict: ...
def _read_session_sidecar(session_id: str) -> dict: ...
def _build_agent_key(source: str, model: str) -> str: ...
def _infer_trace_mode_for_session(session_id: str) -> TraceMode: ...
def _build_trace_native(session_id: str, limit: int, compact: bool) -> MissionControlAgentTraceSnapshot | None: ...
def _build_trace_from_transcript(session_id: str, limit: int) -> MissionControlAgentTraceSnapshot | None: ...
def _fallback_unavailable_trace(session_id: str, reason: str) -> MissionControlAgentTraceSnapshot: ...
```

### Data source precedence

#### For sessions/agents registry
1. `~/.hermes/sessions/sessions.json`
2. `~/.hermes/sessions/session_<id>.json`
3. optional `SessionDB._get_session_rich_row()` / `list_sessions_rich()` to enrich title/preview/message count

#### For trace
1. native: `SessionDB.get_messages(session_id)`
2. transcript/session artifact fallback
3. unavailable payload with warning

---

## File-by-file implementation plan

### Task 1: Add backend schema models

**Objective:** Introduce stable response models for Agents endpoints.

**Files:**
- Modify: `~/.hermes/hermes-agent/hermes_cli/web_server.py`

**Step 1: Add failing smoke expectation note**

Document target endpoints and payload keys in comments/tests before implementation:
- `/api/mission-control/agents`
- `/api/mission-control/sessions`
- `/api/mission-control/agents/trace`

**Step 2: Add Pydantic models**

Insert the models from the schema section above near existing `BaseModel` classes.

**Step 3: Run syntax check**

Run: `python -m py_compile ~/.hermes/hermes-agent/hermes_cli/web_server.py`
Expected: no output

**Step 4: Commit**

```bash
git -C ~/.hermes/hermes-agent add hermes_cli/web_server.py
git -C ~/.hermes/hermes-agent commit -m "feat: add mission control agents response schemas"
```

---

### Task 2: Create Mission Control Agents adapter module

**Objective:** Isolate all file/DB fallback logic behind one backend module.

**Files:**
- Create: `~/.hermes/hermes-agent/hermes_cli/mission_control_agents.py`

**Step 1: Implement durable session file readers**

Add helpers for:
- `~/.hermes/sessions/sessions.json`
- `~/.hermes/sessions/session_<id>.json`
- optional transcript discovery (`<session_id>.jsonl`, `session_<id>.json` if relevant)

**Step 2: Add stable timestamp helpers**

Add:

```python
def _isoformat_or_none(value) -> str | None:
    ...

def _is_live(last_active_ts: float | None, ended_at: float | None, live_window_seconds: int) -> bool:
    ...
```

**Step 3: Add `load_agents_sessions_snapshot()`**

Behavior:
- iterate `sessions.json`
- enrich from `session_<id>.json`
- if available, enrich from `SessionDB._get_session_rich_row(session_id)`
- compute:
  - `agentId`
  - `status`
  - `traceMode`
  - `preview`

**Step 4: Add `load_agents_snapshot()`**

Aggregate sessions by `agentId` and compute:
- `totalSessions`
- `liveSessions`
- `lastActiveAt`
- most permissive useful `traceMode` per agent:
  - any native -> `native`
  - else any transcript -> `transcript`
  - else `unavailable`

**Step 5: Run syntax check**

Run: `python -m py_compile ~/.hermes/hermes-agent/hermes_cli/mission_control_agents.py`
Expected: no output

**Step 6: Commit**

```bash
git -C ~/.hermes/hermes-agent add hermes_cli/mission_control_agents.py
git -C ~/.hermes/hermes-agent commit -m "feat: add mission control agents adapter"
```

---

### Task 3: Add transcript fallback trace builder

**Objective:** Always return a usable trace payload even without native deep trace.

**Files:**
- Modify: `~/.hermes/hermes-agent/hermes_cli/mission_control_agents.py`

**Step 1: Implement transcript parser**

Handle transcript-like artifacts conservatively. Build event types only when evidence exists:
- `user_message`
- `assistant_message`
- `tool_call_started` if assistant message contains `tool_calls`
- `tool_call_completed` if tool role message exists
- `skill_used` only when tool name is one of skill tools (`skill_view`, `skills_list`, `skill_manage`)

**Step 2: Build normalized stats**

Compute:
- turns
n- toolCalls
- skills
- thoughts
- errors
- durationSeconds

**Step 3: Add unavailable fallback**

If no transcript or parse fails, return:

```python
MissionControlAgentTraceSnapshot(
    traceMode="unavailable",
    warnings=[reason],
    events=[],
    nodes=[],
    edges=[],
)
```

**Step 4: Verify behavior with one known session**

Run a tiny probe in Python against a real session id from `sessions.json`.
Expected: valid JSON-ish payload, no exception.

**Step 5: Commit**

```bash
git -C ~/.hermes/hermes-agent add hermes_cli/mission_control_agents.py
git -C ~/.hermes/hermes-agent commit -m "feat: add transcript fallback for agent traces"
```

---

### Task 4: Add best-effort native trace builder

**Objective:** Use `SessionDB.get_messages(session_id)` when available, but never let it be the only path.

**Files:**
- Modify: `~/.hermes/hermes-agent/hermes_cli/mission_control_agents.py`

**Step 1: Build native adapter around `SessionDB.get_messages()`**

Rules:
- wrap in `try/except`
- if import or query fails, return `None`
- do not leak raw exceptions to API callers

**Step 2: Normalize native messages to stable event schema**

Use current safe fields only:
- `role`
- `content`
- `tool_calls`
- `tool_call_id`
- `tool_name`
- `timestamp`
- `reasoning`

Do **not** assume future-only fields like `tool_request` or `tool_response` exist.

**Step 3: Add compact mode**

If `compact=True`, truncate `detail/request/response` safely for UI live mode.

**Step 4: Verify fallback chain**

Cases to test manually:
1. native works -> `traceMode=native`
2. native fails but transcript exists -> `traceMode=transcript`
3. both fail -> `traceMode=unavailable`

**Step 5: Commit**

```bash
git -C ~/.hermes/hermes-agent add hermes_cli/mission_control_agents.py
git -C ~/.hermes/hermes-agent commit -m "feat: add best-effort native agent trace builder"
```

---

### Task 5: Expose new backend endpoints on `9119`

**Objective:** Serve the contract from the dashboard backend instead of inventing frontend fallbacks.

**Files:**
- Modify: `~/.hermes/hermes-agent/hermes_cli/web_server.py`

**Step 1: Import adapter functions**

```python
from hermes_cli.mission_control_agents import (
    load_agents_snapshot,
    load_agents_sessions_snapshot,
    load_agent_trace_snapshot,
)
```

**Step 2: Add endpoints**

```python
@app.get("/api/mission-control/agents")
async def get_mission_control_agents():
    return load_agents_snapshot().model_dump(by_alias=True)

@app.get("/api/mission-control/sessions")
async def get_mission_control_sessions(limit: int = 100):
    return load_agents_sessions_snapshot(limit=limit).model_dump(by_alias=True)

@app.get("/api/mission-control/agents/trace")
async def get_mission_control_agents_trace(session_id: str, limit: int = 300, compact: bool = False):
    return load_agent_trace_snapshot(session_id=session_id, limit=limit, compact=compact).model_dump(by_alias=True)
```

**Step 3: Run syntax check**

Run: `python -m py_compile ~/.hermes/hermes-agent/hermes_cli/web_server.py ~/.hermes/hermes-agent/hermes_cli/mission_control_agents.py`
Expected: no output

**Step 4: Commit**

```bash
git -C ~/.hermes/hermes-agent add hermes_cli/web_server.py hermes_cli/mission_control_agents.py
git -C ~/.hermes/hermes-agent commit -m "feat: expose mission control agents endpoints"
```

---

### Task 6: Upgrade frontend API layer to the stable contract

**Objective:** Stop returning fake trace payloads and teach the frontend the real contract.

**Files:**
- Modify: `~/.hermes/hermes-agent/apps/mission-control/src/lib/hermes-api.ts`

**Step 1: Add TS types from this plan**

Keep the old session snapshot types if other routes need them, but add dedicated Agents contract types.

**Step 2: Add API loaders**

```ts
export async function loadMissionControlAgents(accessToken?: string): Promise<MissionControlAgentsSnapshot> { ... }
export async function loadMissionControlAgentSessions(accessToken?: string): Promise<MissionControlAgentsSessionsSnapshot> { ... }
export async function loadMissionControlAgentTrace(sessionId?: string, accessToken?: string, limit = 300, compact = false): Promise<MissionControlAgentTraceSnapshot> { ... }
```

**Step 3: Replace fake `loadMissionControlAgentTrace()`**

Current code at `hermes-api.ts:1423-1430` is hardcoded fallback trash. Replace it.

**Step 4: Add fallback objects matching new schema**

Include `traceMode` and `warnings` in fallbacks.

**Step 5: Run build**

Run: `npm --prefix ~/.hermes/hermes-agent/apps/mission-control run build`
Expected: build succeeds

**Step 6: Commit**

```bash
git -C ~/.hermes/hermes-agent add apps/mission-control/src/lib/hermes-api.ts
git -C ~/.hermes/hermes-agent commit -m "feat: add upgrade-safe agents api client"
```

---

### Task 7: Refactor `AgentsRoute.tsx` to use backend agent/session contracts

**Objective:** Stop deriving agent aggregates purely from generic session snapshots.

**Files:**
- Modify: `~/.hermes/hermes-agent/apps/mission-control/src/routes/AgentsRoute.tsx`

**Step 1: Remove local aggregate model derived from generic sessions**

Current local `AgentAggregate` is okay for a prototype, not for the real contract.

**Step 2: Load agents + agent sessions from backend**

Create state for:
- `agentsSnapshot`
- `agentSessionsSnapshot`
- `trace`

**Step 3: Keep existing UI affordances**

Preserve:
- timeline default
- DAG secondary
- live mode toggle
- session selector
- event modal

But drive them from the new payloads.

**Step 4: Respect `traceMode` in UI**

Behavior:
- `native` -> normal UI
- `transcript` -> show badge `degraded trace`
- `unavailable` -> disable DAG, show plain warning banner

**Step 5: Run build**

Run: `npm --prefix ~/.hermes/hermes-agent/apps/mission-control run build`
Expected: build succeeds

**Step 6: Commit**

```bash
git -C ~/.hermes/hermes-agent add apps/mission-control/src/routes/AgentsRoute.tsx
Git -C ~/.hermes/hermes-agent commit -m "feat: wire agents route to stable backend contract"
```

Note: yes, fix the capital `Git` if you copy-paste blindly. That would be a very on-brand self-own.

---

### Task 8: Add backend tests for fallback ladder

**Objective:** Prove the contract survives missing DB/native trace.

**Files:**
- Create: `~/.hermes/hermes-agent/tests/hermes_cli/test_mission_control_agents.py`

**Step 1: Add tests for agents registry from filesystem index**

Cases:
- sessions index exists
- sidecar file exists
- no DB needed

**Step 2: Add tests for trace mode ladder**

Cases:
- native trace success -> `traceMode == 'native'`
- native import/query failure + transcript present -> `traceMode == 'transcript'`
- both unavailable -> `traceMode == 'unavailable'`

**Step 3: Add endpoint tests**

If existing FastAPI test harness exists, verify:
- `/api/mission-control/agents`
- `/api/mission-control/sessions`
- `/api/mission-control/agents/trace`

**Step 4: Run tests**

Run: `pytest ~/.hermes/hermes-agent/tests/hermes_cli/test_mission_control_agents.py -q`
Expected: pass

**Step 5: Commit**

```bash
git -C ~/.hermes/hermes-agent add tests/hermes_cli/test_mission_control_agents.py
git -C ~/.hermes/hermes-agent commit -m "test: cover mission control agents fallback contract"
```

---

### Task 9: Add frontend tests or at least contract-safe smoke checks

**Objective:** Make sure the UI does not explode when `traceMode` degrades.

**Files:**
- Create or modify appropriate test file under `apps/mission-control/src/`

**Step 1: Add minimal normalization tests**

Test that `normalizeTraceMode()` defaults to `unavailable`.

**Step 2: Add route render tests if test harness exists**

Cases:
- native trace
- transcript trace
- unavailable trace

**Step 3: Run frontend tests/build**

Run whichever test command exists; at minimum:

```bash
npm --prefix ~/.hermes/hermes-agent/apps/mission-control run build
```

**Step 4: Commit**

```bash
git -C ~/.hermes/hermes-agent add apps/mission-control/src
git -C ~/.hermes/hermes-agent commit -m "test: harden agents ui against degraded trace modes"
```

---

## Verification checklist

Before calling this done:

- [ ] `GET /api/mission-control/agents` returns real data from session artifacts
- [ ] `GET /api/mission-control/sessions` returns normalized session rows with `agentId`, `status`, `traceMode`
- [ ] `GET /api/mission-control/agents/trace?session_id=...` never returns hardcoded fallback garbage when real session data exists
- [ ] If `SessionDB` breaks or changes, trace endpoint degrades to `transcript` or `unavailable` but still returns `200`
- [ ] Agents UI still renders with `traceMode=transcript`
- [ ] Agents UI does not crash with `traceMode=unavailable`
- [ ] `npm --prefix apps/mission-control run build` passes
- [ ] backend py_compile passes
- [ ] tests for fallback ladder pass

---

## Pitfalls to avoid

1. **Do not let the frontend assemble agent identity by guessing from old session shapes forever.**
2. **Do not make DAG mandatory.** When trace is degraded, DAG should yield gracefully.
3. **Do not assume `SessionDB.get_messages()` schema is forever.** Treat it as best-effort.
4. **Do not assume sidecar/transcript file names from vibes.** Probe conservatively, support missing files.
5. **Do not return raw home-directory paths to the UI.** Redact to `~` style.
6. **Do not let a native-trace exception bubble up as HTTP 500.** That defeats the whole point.
7. **Do not store Mission Control-only assumptions in the frontend.** Keep the adapter backend as the single normalization boundary.

---

## Recommended execution order

1. Backend schemas
2. Backend adapter module
3. Filesystem-backed sessions snapshot
4. Transcript fallback trace
5. Native best-effort trace
6. Web server endpoints
7. Frontend API layer
8. Agents route wiring
9. Tests + smoke

That order keeps the feature useful early and pretty later. Exactly how software should work when you don’t want updates to punch you in the throat.

---

## Handy smoke commands

```bash
python -m py_compile ~/.hermes/hermes-agent/hermes_cli/web_server.py ~/.hermes/hermes-agent/hermes_cli/mission_control_agents.py
pytest ~/.hermes/hermes-agent/tests/hermes_cli/test_mission_control_agents.py -q
curl -s http://127.0.0.1:9119/api/mission-control/agents | jq
curl -s 'http://127.0.0.1:9119/api/mission-control/sessions?limit=20' | jq
curl -s 'http://127.0.0.1:9119/api/mission-control/agents/trace?session_id=20260424_094245_a263f3' | jq
npm --prefix ~/.hermes/hermes-agent/apps/mission-control run build
```

---

## End state

When this is implemented correctly:

- Agents page works from durable session artifacts even after Hermes core updates.
- Deep trace gets better when core internals cooperate, worse when they don’t, but never catastrophic.
- Mission Control owns its contract.
- The frontend stops living on fake fallback payloads and starts acting like an actual observability tool.
