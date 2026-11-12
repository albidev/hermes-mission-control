# Private synthetic drawer regression fixture

Run from the private candidate source, with a known read-only `playwright-core` module and Chrome installed:

```sh
PLAYWRIGHT_MODULE=/absolute/path/to/playwright-core/index.mjs \
  npm run test:room-creation-browser -- "$PWD" "$PWD/../implementation/fresh-browser-run" final
```

The guarded source/output layout is intentional: this fixture must not accidentally start against a serving worktree or overwrite completed evidence. Output must be a **new** directory below `rooms-create-dialog/implementation/`, paired with its sibling `source/`. No port is fixed; forbidden production ports are rejected. No dependencies are installed by this command.

`fixture.tsx` renders the **actual production ChatDrawer**, hence the real header, mode rail, room transcript, create layer, CreateRoomForm and embedded InviteLobbyCreateForm. It does not copy or redraw those surfaces. The server substitutes only the roster-loading data boundary; GroupGatewayClient methods are replaced with synthetic in-memory functions. All `fetch` responses are synthetic in-page stubs; WebSocket is forbidden. Browser routing blocks external origins and every actual `/api/` HTTP request; Vite has no proxy. All profiles, rooms, peers, vaults, callback payloads and model descriptions are invented fixture data, not live records. No model picker RPC or real model/provider switch is performed.

The matrix includes 320×568, 390×844, 768×1024, 1280×720, 1440×900, 859×1217, 844×390, 390×340 and 320×300. 1440 desktop uses the actual narrow 540px drawer. The production resize handle is exercised, then the persisted width is tested at mobile sizes. There is no unsupported expanded-drawer variant.

Assertions cover local drawer center/bounds, one content scroller, one active X/no Cancel, selected context, Escape propagation, real Tab/Shift-Tab, focus return, background pointer/focus blocking, nondismissing scrim, outside-page interaction, full-roster bulk semantics, search/draft preservation, pending/error/empty/invite states, min-44px controls, available theme contrast and Italian labels. `ownership.json`, `server.json`, `result.json`, PNGs and finally teardown are written per fresh run. Browser profile and server are owned by that run; the server PID birth is checked before termination.

Evidence is **desktop Chrome with resized viewports and synthesized mouse/keyboard input**. Short heights approximate keyboard space; they are not physical iPhone, Android, Safari/WebKit, touch, IME, screen-reader or native backend full-flow evidence. VisualViewport fitting also has an isolated unit test.
