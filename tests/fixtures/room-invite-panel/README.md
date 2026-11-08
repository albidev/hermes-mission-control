# RoomInvitePanel browser lane

Optional maintained lane; it is intentionally not part of `scripts/run-frontend-tests.mjs` because it uses an already-installed local Chrome and `playwright-core` only.

From `mc/`, run:

```sh
PLAYWRIGHT_MODULE=/absolute/path/to/playwright-core/index.mjs \
CHROME_PATH=/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
node tests/fixtures/room-invite-panel/browser.mjs
```

The fixture starts a Vite server on loopback, mounts the actual `RoomInvitePanel`, blocks non-local requests, and asserts long content at 320px portrait and landscape, inner width containment, label containment, and 44px Close/Copy targets. No screenshots or secrets are written.
