# P3b-UI.3 — Quality pass

## Components and integration

- Rewritten `PairingSection` + `PeerList`: readable JSX, host offer/accept/reveal and joining import/reveal paths, numeric six-digit SAS, countdown, cancellation, cleared ephemeral codes, truthful pending acknowledgement state. Removing peers uses `/peers/{peer_id}`, not `/pairing/peers/{peer_id}`.
- Rewritten `ReceivedInvitationPanel`: paired identity, metadata, redeem, abortable sequential polling with bounded exponential backoff, exact room/member/permissions/catalog/policy review, scope confirmation and a separate consent action, activation polling and canonical returned room opening. Closing/unmounting aborts polling. Consent never sends a prompt.
- Rewritten `GuestRoomView` split into `GuestBotPanel`/`GuestTranscript`. My bot is the mobile default; desktop is 340px bot column plus read-only transcript. Approvals use 137×44px targets and a measured 12px gap at 390px. Offline is a themed alert with icon/time/background.
- `RoomTranscriptRow` is shared by **both** GroupRoomView and GuestTranscript; it delegates to the existing ChatMessageCard. Gateway-owned state, filtering, round dividers, tool strips and composer remain in GroupRoomView. Guest view has no composer or gateway mutation controls.
- `BottomSheet`: full-height mobile, safe-area bottom inset, portal, focus containment/cycling/restoration, Escape and body-scroll lock. `SegmentedControl`: tablist/tab, roving tabindex, Left/Right/Home/End. `Stepper`: vertical, current step and completed states, only current content expanded.
- ConfigRoute feature-gates pairing. ChatDrawer feature-gates invitations and guest discovery, renders guests in the regular room picker with lock/badge, and renders guest content instead of the local composer. The invitation badge counts the currently imported in-memory invitation (0/1); no backend collection/count endpoint exists in the frozen contract. It is not a fabricated guest-room count.
- All new UI labels use matching `inviteUi.*` it/en keys; server-supplied names, IDs, permissions and catalog remain verbatim. New CSS uses repository theme tokens. No rewritten screen/primitives JSX line exceeds 140 characters.

## Contracts

### Pairing — contract: P3b.1a-2 must match

Base `/api/local/room-invites`. POST `/pairing/offers` → `{offer:{id,code,expires_at}}`; POST `/pairing/offers/{id}/accept` `{code}` → `{reveal:{id,code,sas,expires_at}}`; POST confirm `{sas}` → `{pairing}`; POST cancel `{}`. POST `/pairing/imports` `{code}` → `{accept:{id,code,identity,expires_at}}`; POST import reveal `{code}` → `{id,sas,expires_at}`; POST import confirm `{sas}` → `{pairing}`. GET `/pairing` → `{pairings:[{id,role,state,expires_at,peer?}]}`. DELETE `/peers/{peer_id}`. Confirmed is not acknowledged/operational. Joining cancellation clears the screen locally: no joining-cancel route is specified.

### Joining

POST `/imports/preview` `{code,host_id?}`, POST `/{id}/metadata`, POST `/{id}/redeem`, GET `/{id}`, POST `/{id}/scope` `{room_id,member_id}`, POST `/{id}/consent` `{approve}`, POST `/{id}/resolve`. Responses use `{import:JoiningImport}` with optional paired host, metadata, exact scope and active `room_id`. Polls use GET, never resolve or consent automatically. Backend rejection/expiry terminates the flow. Cancellation is local because no cancellation route exists.

### Guest — PROVISIONAL, not claimed as backend integration

GET `/guest-rooms` → `{rooms:GuestRoom[]}`; GET `/{room_id}/log?since_seq=0&limit=100` → verbatim `{events:[{seq,event_id,kind,actor,authority_epoch,payload,created_at}],cursor,latest_seq,has_more,authority:{gateway_id,epoch}}`; GET `/my-bot`; POST `/approvals/{request_id}` `{choice}`; POST `/leave` `{}`. Next page uses max **received** seq, not `latest_seq`. Overlapping event IDs keep the first immutable event; rows sort by original seq. No browser persistence. All clients explicitly use no-store, same-origin credentials, owner auth and abort signals. 404 hides the section; 401/403/409 and generic HTTP failures expose only typed generic codes.

## Verification and evidence

- `green/tsc.log` / `.exit`: TypeScript project check.
- `green/build.log` / `.exit`: Vite production build; existing large-chunk warning only.
- `green/frontend.log` / `.exit`: all frontend files discovered by the repo runner; 121 files, including real mounted React/jsdom scenarios (45 direct behavioral checks plus enabled-control assertions), API transport tests, full pairing transitions, joining backoff/cleanup, guest immutable ordering and feature-off tests. More than 40 meaningful assertions.
- `green/server.log`: unchanged server suite rerun, **428 tests OK (1 skipped)**. No server files changed.
- `red/frontend.log` / `.exit`: same seven UI test files copied to a disposable `git archive 505c13a` plus node_modules symlink; exit 1. Baseline lacks the new feature modules (module-not-found) and fails actual mounting/DEV guard assertions; this is not a claim of 40 behavioral failures on nonexistent code.
- `green/browser.json`: real disposable Chrome/CDP interactions, not static HTML screenshots. Vite only `127.0.0.1:5180 --strictPort`, fixture APIs via `?mock=invite-rooms`. Host pairing and invitation through consent/activation exercised end-to-end. Guest pagination overlap/order and no composer verified. All screenshots have zero horizontal overflow/clipped header controls and no JS errors/missing translations. Guest Chat additionally checked at 375/390/430.
- `green/mock-exclusion.log`: production assets scanned for mock module/query/fixture markers; no matches.
- `green/port-5180.log`: empty after teardown (`lsof` exit 1 means no listener). Owned Vite and disposable Chrome are stopped in finally.

## Eight screenshots — every PNG opened with vision

| File | Viewport | Checked |
| --- | --- | --- |
| `screens/pairing-step-1.png` | 390×844 | Full-screen one-time offer sheet; readable code, expiry and full-width copy control. Intentional space for real long codes. |
| `screens/pairing-sas.png` | 390×844 | Active step scrolled into view; SAS, numeric input, security warning and full confirmation action. |
| `screens/peer-list.png` | 1440×900 | Ready vs pending confirmation, readable identity and remove action; desktop navigation labels no awkward wrapping. |
| `screens/invitation-consent.png` | 390×844 | Active step scrolled into view; scope already confirmed, separate consent button fully visible. |
| `screens/guest-my-bot.png` | 390×844 | My bot default, hierarchy, approval detail, separate 44px Approve/Reject controls, permissions/members. |
| `screens/guest-chat.png` | 390×844 | Shared canonical rows, event seq, read-only owner banner, no composer, load-next action. |
| `screens/guest-desktop.png` | 1440×900 | Left 340px bot panel, right transcript, room identity and full labels. |
| `screens/offline.png` | 390×844 | Red background alert, warning icon and 14:02 timestamp, wrapped disclaimer; last received snapshot only. |

## Limits kept explicit

Guest transport remains provisional and the screenshots use injectable DEV fixtures; no live room-network deployment or real-owner mutation was performed. No new backend routes were invented for cancellation or invitation collection/count. Pending badges are only session-local, not a durable server inbox. No install, push or PR. Local commits use the requested author. Evidence is mirrored to the original packet evidence directory.
