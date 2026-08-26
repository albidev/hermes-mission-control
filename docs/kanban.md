# Kanban

Mission Control includes a responsive Kanban board for operating Hermes task boards from the dashboard.

The feature is available from the **Kanban** route in the side navigation. It supports desktop and mobile layouts and reuses the Hermes core `kanban_db` implementation through the local telemetry sidecar. Mission Control does not duplicate the task database or dispatcher logic.

## Capabilities

- Eight workflow columns: `triage`, `todo`, `scheduled`, `ready`, `running`, `blocked`, `review`, and `done`
- Board picker with the current CLI/gateway board indicator
- Create boards with:
  - slug (lowercase, hyphen-separated identifier)
  - display name
  - description
  - project directory (`default_workdir`)
  - icon or emoji
  - optional switch-to-board behavior
- Archive boards or permanently delete them after confirmation
- Protection for the default board and the last remaining board
- Create tasks with:
  - target column
  - title and description
  - specifier/assignee
  - priority
  - comma-separated skills
  - workspace kind (`scratch`, `worktree`, or `dir`)
  - workspace path
  - optional parent task
  - goal mode
- Drag and drop tasks between columns with optimistic UI and rollback on failure
- Task drawer with task details and comments
- Operational task inspector with:
  - result/final summary
  - child task relationships
  - run history (profile, outcome, elapsed time, summaries, errors)
  - event/activity timeline with structured payloads
  - bounded worker log tail
  - status actions (ready, block, complete, archive)
  - assignee and priority editing
  - parent dependency linking and unlinking
- Search by task title, ID, or assignee
- Tenant and assignee filters
- Live board refresh through event-tail polling
- Rich task cards with priority, short ID, relative age, assignee, progress, and comments

## Desktop and mobile behavior

- Desktop uses equal-width columns in a horizontally scrollable board.
- Mobile keeps the same column width so cards remain readable and the board scrolls horizontally instead of compressing into unusable slivers.
- Creation and confirmation dialogs become bottom sheets on mobile.
- Dialogs have internal scrolling and safe-area padding for iOS.
- Form inputs use a mobile-safe font size to avoid Safari auto-zoom.
- Custom dropdowns share the board picker interaction pattern: visible chevron, selected-item indicator, outside-click dismissal, and upward opening inside constrained dialogs.

## Architecture

The frontend calls the sidecar endpoints under `/api/local/kanban/*`:

| Endpoint | Purpose |
|---|---|
| `GET /api/local/kanban/boards` | List boards and current-board state |
| `GET /api/local/kanban/board` | Load the active board and columns |
| `GET /api/local/kanban/tasks/:id` | Load task details, comments, runs, and events |
| `GET /api/local/kanban/events` | Read board events after a cursor |
| `POST /api/local/kanban/boards` | Create a board |
| `POST /api/local/kanban/boards/:slug/switch` | Switch the CLI/gateway board |
| `POST /api/local/kanban/boards/:slug/delete` | Archive or permanently delete a board |
| `POST /api/local/kanban/tasks` | Create a task |
| `POST /api/local/kanban/tasks/:id/move` | Move a task between columns |
| `POST /api/local/kanban/tasks/:id/comments` | Add a comment |

The bridge delegates database and task lifecycle operations to the Hermes core `kanban_db`. Orchestration settings remain in the Hermes core configuration and dashboard; Mission Control deliberately does not duplicate that settings panel.

## Localization

The UI supports English (`en`) and Italian (`it`) through the shared `I18nProvider` and catalogs in `src/locales/en.json` and `src/locales/it.json`.

- The language switcher is available in the global shell and persists the selected locale in `localStorage`.
- All user-facing dashboard, Kanban, Chat, canvas, dialog, tooltip, placeholder, and accessibility labels resolve through the active catalog.
- Dates, times, numbers, percentages, and currencies use locale-aware formatters from `src/lib/format.ts`.
- Missing keys fall back to English; missing keys in both catalogs remain visible as their key name rather than silently disappearing.
- Product names, model names, IDs, paths, slugs, file formats, and user-generated content are intentionally not translated.

## Tool inventory

The Tools route reads configurable toolsets from the Hermes installation resolved through `HERMES_HOME`, rather than relying on the Mission Control repository layout. This keeps the inventory accurate when Hermes is installed outside the dashboard checkout.


The bridge tests use an isolated temporary `HERMES_HOME` and the real Hermes core checkout. They run locally with the core virtual environment:

```bash
~/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_kanban_bridge.py -q
```

CI skips these host-dependent bridge tests when the Hermes core checkout is unavailable, while still running the remaining Python tests and the frontend build.
