import { createRequire } from 'node:module';
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

const require = createRequire(import.meta.url);

export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, process.cwd(), ''), ...process.env };
  const LOCAL_TELEMETRY_TARGET = env.MISSION_CONTROL_LOCAL_TELEMETRY_URL || 'http://127.0.0.1:8765';
  const DASHBOARD_HOST = env.MISSION_CONTROL_DASHBOARD_HOST || '127.0.0.1';
  const rawDashboardPort = env.MISSION_CONTROL_DASHBOARD_PORT || '9119';
  if (!/^\d+$/.test(rawDashboardPort) || Number(rawDashboardPort) < 1 || Number(rawDashboardPort) > 65535) {
    throw new Error(`[vite-config][FAIL] invalid dashboard port: ${rawDashboardPort}`);
  }
  const DASHBOARD_PORT = Number(rawDashboardPort);
  const DASHBOARD_TARGET = env.HERMES_DASHBOARD_URL || `http://${DASHBOARD_HOST}:${DASHBOARD_PORT}`;
  const ALLOWED_HOSTS = (env.MISSION_CONTROL_ALLOWED_HOSTS || 'localhost,127.0.0.1')
    .split(',')
    .map((host) => host.trim())
    .filter(Boolean);

  const DEV_SERVER_HOSTS = (env.MISSION_CONTROL_DEV_HOSTS || '')
    .split(',')
    .map((h) => h.trim())
    .filter(Boolean);

  const ALL_ALLOWED_HOSTS = [...new Set([...ALLOWED_HOSTS, ...DEV_SERVER_HOSTS])];
  // External plugin UIs are symlinked under src/plugins/ but their source files
  // live outside the MC package. Resolve shared peer dependencies from MC's
  // own node_modules so plugins do not need a second React installation.
  const hostReact = require.resolve('react', { paths: [process.cwd()] });
  const hostReactDom = require.resolve('react-dom', { paths: [process.cwd()] });
  const hostRouter = require.resolve('react-router-dom', { paths: [process.cwd()] });
  const hostIcons = require.resolve('lucide-react', { paths: [process.cwd()] });
  const hostReactMarkdown = require.resolve('react-markdown', { paths: [process.cwd()] });
  const hostRemarkBreaks = require.resolve('remark-breaks', { paths: [process.cwd()] });
  const hostRemarkGfm = require.resolve('remark-gfm', { paths: [process.cwd()] });
  // External plugin CSS entries (e.g. `@import 'tailwindcss/theme'`) live
  // outside the repo tree via the src/plugins symlink. PostCSS/Vite's CSS
  // resolver walks up from the symlink's real path and never finds the
  // host's node_modules, so bare `tailwindcss/*` subpath imports 404 with
  // ENOENT. Alias the subpaths explicitly so any plugin following this
  // pattern resolves against the host's own tailwindcss install.
  const hostTailwindTheme = require.resolve('tailwindcss/theme.css', { paths: [process.cwd()] });
  const hostTailwindUtilities = require.resolve('tailwindcss/utilities.css', { paths: [process.cwd()] });
  const hostTailwindPreflight = require.resolve('tailwindcss/preflight.css', { paths: [process.cwd()] });

  return {
    resolve: {
      dedupe: ['react', 'react-dom'],
      alias: [
        { find: /^react$/, replacement: hostReact },
        { find: /^react-dom$/, replacement: hostReactDom },
        { find: /^react-router-dom$/, replacement: hostRouter },
        { find: /^lucide-react$/, replacement: hostIcons },
        { find: /^react-markdown$/, replacement: hostReactMarkdown },
        { find: /^remark-breaks$/, replacement: hostRemarkBreaks },
        { find: /^remark-gfm$/, replacement: hostRemarkGfm },
        { find: /^tailwindcss\/theme$/, replacement: hostTailwindTheme },
        { find: /^tailwindcss\/utilities$/, replacement: hostTailwindUtilities },
        { find: /^tailwindcss\/preflight$/, replacement: hostTailwindPreflight }
      ],
    },
    plugins: [react()],
    server: {
      host: true,
      port: 5174,
      strictPort: true,
      // Runtime/worktree directories live below the repo root but are not app
      // sources. Watching them turns cleanup and generated state into full-page
      // reloads (especially when a nested worktree is removed).
      // Mission Control is also served to iOS through Tailscale Funnel. Vite's
      // HMR WebSocket is not reliable across mobile sleep/network transitions
      // and can reset the whole React tree. The live app is refreshed by launchd
      // when source/config changes, so keep the remote surface stable.
      hmr: false,
      watch: {
        ignored: ['**/.worktrees/**', '**/.hermes/**', '**/dist/**'],
      },
      // Vite's built-in CORS middleware already answers OPTIONS preflights for
      // /api/* — but its default allowlist only covers localhost, so clients
      // reached through Tailscale serve saw preflights come back without
      // Access-Control-Allow-Origin ("due to access control checks"). Extend
      // the allowlist to the local hosts and the tailnet (the concrete host
      // comes from MISSION_CONTROL_DEV_HOSTS / MISSION_CONTROL_ALLOWED_HOSTS).
      cors: {
        origin: (origin, callback) => {
          if (!origin) return callback(null, false);
          try {
            const host = new URL(origin).hostname;
            const ok = host === 'localhost' || host === '127.0.0.1' || host.endsWith('.local')
              || host.endsWith('.ts.net') || ALL_ALLOWED_HOSTS.includes(host);
            return callback(null, ok);
          } catch { return callback(null, false); }
        },
        methods: 'GET,HEAD,PUT,PATCH,POST,DELETE,OPTIONS',
        allowedHeaders: ['Authorization', 'Content-Type', 'X-API-Key'],
        credentials: true,
        maxAge: 0,
      },
      allowedHosts: ALL_ALLOWED_HOSTS.length ? ALL_ALLOWED_HOSTS : true,
      proxy: {
        '/api/local': {
          target: LOCAL_TELEMETRY_TARGET,
          changeOrigin: true,
          rewrite: (path) => path,
        },
        '/api/terminal': {
          target: 'http://127.0.0.1:8766',
          changeOrigin: true,
          ws: true,
          rewriteWsOrigin: true,
        },
        '/api/gateway-root': {
          target: DASHBOARD_TARGET,
          changeOrigin: true,
          rewrite: () => '/',
        },
        // Dashboard authentication remains server-authoritative. Mission Control
        // only keeps the login page and OAuth/password callbacks same-origin so
        // the server can mint an identity-bearing WebSocket ticket afterwards.
        '/login': {
          target: DASHBOARD_TARGET,
          changeOrigin: true,
        },
        '/auth': {
          target: DASHBOARD_TARGET,
          changeOrigin: true,
        },
        '/api': {
          target: DASHBOARD_TARGET,
          changeOrigin: true,
          ws: true,
          rewriteWsOrigin: true,
        },
      },
    },
    // vite preview (used by `pnpm preview`) also needs the LAN/tailnet hosts
    // allowlisted. Otherwise a preview process answering on 5174 returns the
    // "Blocked request. This host is not allowed." page for tailnet clients,
    // which masquerades as a Mission Control outage. Keep this in sync with
    // the server.allowedHosts list above.
    preview: {
      host: true,
      port: 4174,
      strictPort: true,
      allowedHosts: ALL_ALLOWED_HOSTS.length ? ALL_ALLOWED_HOSTS : true,
    },
  };
});
