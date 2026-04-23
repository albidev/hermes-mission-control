import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react-swc';

const LOCAL_TELEMETRY_TARGET = process.env.MISSION_CONTROL_LOCAL_TELEMETRY_URL || 'http://127.0.0.1:8765';
const ALLOWED_HOSTS = (process.env.MISSION_CONTROL_ALLOWED_HOSTS || 'localhost,127.0.0.1')
  .split(',')
  .map((host) => host.trim())
  .filter(Boolean);

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5174,
    strictPort: true,
    allowedHosts: ALLOWED_HOSTS,
    proxy: {
      '/api/local': {
        target: LOCAL_TELEMETRY_TARGET,
        changeOrigin: true,
      },
      '/api': {
        target: process.env.HERMES_DASHBOARD_URL || 'http://127.0.0.1:9119',
        changeOrigin: true,
      },
    },
  },
  preview: {
    host: true,
    port: 4174,
    strictPort: true,
    proxy: {
      '/api/local': {
        target: LOCAL_TELEMETRY_TARGET,
        changeOrigin: true,
      },
      '/api': {
        target: process.env.HERMES_DASHBOARD_URL || 'http://127.0.0.1:9119',
        changeOrigin: true,
      },
    },
  },
});
