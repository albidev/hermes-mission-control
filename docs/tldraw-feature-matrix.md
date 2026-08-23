# TLDrawCanvas feature matrix

Scope: Mission Control's session-bound `TLDrawCanvas`.

Legend:

- **Built-in**: provided by the mounted `<Tldraw />` surface and usable directly by the user.
- **Bridge**: callable by the authenticated Mission Control whiteboard bridge.
- **Verified**: exercised by a build, API test, or live snapshot readback.

## Canvas core

| Feature | Built-in | Bridge | Status |
|---|---:|---:|---|
| Pan / hand tool | yes | n/a | Built-in |
| Zoom in/out | yes | `zoom_to_fit` | Bridge + built-in |
| Zoom to fit | yes | `zoom_to_fit` | Implemented |
| Single/multi selection | yes | selection readback | Implemented |
| Marquee selection | yes | n/a | Built-in |
| Undo / redo | yes | editor history | Built-in |
| Cut/copy/paste | yes | pending | Built-in |
| Duplicate | yes | `duplicate` | Implemented |
| Delete | yes | `delete_shapes`, `clear` | Implemented |
| Group / ungroup | yes | `group`, `ungroup` | Implemented |
| Bring front / send back | yes | `bring_to_front`, `send_to_back` | Implemented |

## Shape tools

| Feature | Built-in | Bridge | Status |
|---|---:|---:|---|
| Select | yes | n/a | Built-in |
| Draw / freehand | yes | generic `create_shape` | Built-in + generic bridge |
| Eraser | yes | n/a | Built-in |
| Arrow | yes | `create_arrow` | Implemented |
| Line | yes | `create_line` | Implemented |
| Text | yes | `create_text`, generic shape | Implemented |
| Note | yes | generic `create_shape` | Built-in + generic bridge |
| Geo shapes | yes | `create_box`, generic shape | Implemented |
| Frame | yes | `create_frame` | Implemented |
| Image / video / embed | yes | generic shape, upload path pending | Built-in |

## Connectors

| Feature | Built-in | Bridge | Status |
|---|---:|---:|---|
| Arrowheads | yes | `create_arrow` | Implemented |
| Shape bindings | yes | `create_binding` | Implemented |
| Normalized anchors | yes | binding props | Implemented |
| Curved routing | yes | arrow props | Implemented |
| Labels | yes | arrow rich text props | Bridge-ready |
| Semantic colors | yes | command colors | Implemented |

## Text and styling

| Feature | Built-in | Bridge | Status |
|---|---:|---:|---|
| Fonts / sizes | yes | generic props | Bridge-ready |
| Colors / fills | yes | generic props | Bridge-ready |
| Solid / dashed lines | yes | generic props | Bridge-ready |
| Alignment | yes | generic props | Bridge-ready |
| Opacity | yes | generic props | Bridge-ready |
| Rich text | yes | `toRichText` / generic props | Implemented |

## Organization and persistence

| Feature | Built-in | Bridge | Status |
|---|---:|---:|---|
| Pages | yes | `create_page`, `set_current_page`, `rename_page`, `delete_page`, `move_shapes_to_page` | Implemented |
| Frames / layers | yes | `create_frame` | Implemented |
| Snapping / alignment / distribution | yes | `align_shapes`, `distribute_shapes`, `pack_shapes` | Implemented |
| Lock / hide / crop / rotate / resize | yes | `toggle_lock`, `flip_shapes`, `rotate_shapes`, `resize_shape` | Partially implemented |
| Style / opacity | yes | `set_style`, `set_opacity` | Implemented |
| Session isolation | n/a | session ID + stable session key | Implemented + verified |
| Local snapshot | n/a | localStorage | Implemented + verified |
| Server snapshot | n/a | authenticated bridge | Implemented + verified |
| HMR-safe hydration | n/a | remote-before-empty-sync | Implemented |
| Resume migration | n/a | stable-key fallback from legacy session ID | Implemented + verified |
| Geometry recovery | n/a | snapshot sanitizer | Implemented |
| Command version/feature gating | n/a | protocol v2 + feature map | Implemented + verified |

## Export and collaboration

| Feature | Built-in | Bridge | Status |
|---|---:|---:|---|
| PNG / SVG / JSON export | yes | client export menu | Implemented + verified |
| Screenshot to Chat | custom | authenticated image attachment | Implemented + verified |
| Send selection to Chat | custom | current session | Implemented + verified |
| Chat proposal → canvas | custom | command queue | Implemented + verified |
| Agent modes | custom | persisted `draw` / `review` / `arrange` / `explain` | Implemented + verified |
| Board lints | custom | context + header badge | Implemented + verified |

## Verification policy

A feature is only marked implemented when the relevant path is exercised and read back. Shape counts alone are not visual verification; diagrams require a layout/screenshot review as well.
