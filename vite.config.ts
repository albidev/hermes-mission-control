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

  return {
    resolve: {
      dedupe: ['react', 'react-dom'],
      alias: [
        { find: /^react$/, replacement: hostReact },
        { find: /^react-dom$/, replacement: hostReactDom },
        { find: /^react-router-dom$/, replacement: hostRouter },
        { find: /^lucide-react$/, replacement: hostIcons }
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
        '/api': {
          target: DASHBOARD_TARGET,
          changeOrigin: true,
          ws: true,
          rewriteWsOrigin: true,
        },
      },
    },
  };
});
