import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const LOCAL_TELEMETRY_TARGET = process.env.MISSION_CONTROL_LOCAL_TELEMETRY_URL || 'http://127.0.0.1:8765';
const DASHBOARD_HOST = process.env.MISSION_CONTROL_DASHBOARD_HOST || '127.0.0.1';
const DASHBOARD_PORT = process.env.MISSION_CONTROL_DASHBOARD_PORT || '9119';
const DASHBOARD_TARGET = process.env.HERMES_DASHBOARD_URL || `http://${DASHBOARD_HOST}:${DASHBOARD_PORT}`;
const ALLOWED_HOSTS = (process.env.MISSION_CONTROL_ALLOWED_HOSTS || 'localhost,127.0.0.1')
  .split(',')
  .map((host) => host.trim())
  .filter(Boolean);

const DEV_SERVER_HOSTS = (process.env.MISSION_CONTROL_DEV_HOSTS || '')
  .split(',')
  .map((h) => h.trim())
  .filter(Boolean);

const ALL_ALLOWED_HOSTS = [...new Set([...ALLOWED_HOSTS, ...DEV_SERVER_HOSTS])];

export default defineConfig({
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
});
