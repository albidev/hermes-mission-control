import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const LOCAL_TELEMETRY_TARGET = process.env.MISSION_CONTROL_LOCAL_TELEMETRY_URL || 'http://127.0.0.1:8765';
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
    allowedHosts: ALL_ALLOWED_HOSTS.length ? ALL_ALLOWED_HOSTS : true,
    proxy: {
      '/api/local': {
        target: LOCAL_TELEMETRY_TARGET,
        changeOrigin: true,
        rewrite: (path) => path,
      },
      '/api/gateway-root': {
        target: process.env.HERMES_DASHBOARD_URL || 'http://127.0.0.1:9119',
        changeOrigin: true,
        rewrite: () => '/',
      },
      '/api': {
        target: process.env.HERMES_DASHBOARD_URL || 'http://127.0.0.1:9119',
        changeOrigin: true,
        ws: true,
        rewriteWsOrigin: true,
      },
    },
  },
});
