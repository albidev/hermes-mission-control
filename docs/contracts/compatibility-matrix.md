# Mission Control compatibility matrix

| Backend behavior | Expected UI behavior |
|---|---|
| `GET /mission-control/capabilities` exists and returns v1 | Full feature gating from server capabilities |
| Capabilities endpoint missing (404) | Fallback to built-in capabilities (v1 defaults) |
| SSE stream works with `event: trace` | Live stream consumed with named listener |
| SSE stream only emits default message events | Live stream consumed via `onmessage` fallback |
| SSE stream unavailable | Automatic polling fallback |
| Trace payload direct shape | Normalized directly |
| Trace payload wrapped under `trace`, `data`, or `payload` | Unwrapped and normalized |
| `compact=1` unsupported | UI can skip compact mode via capabilities |
| Trace payload missing required fields | UI falls back to empty trace (no crash) |
