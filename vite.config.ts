import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react-swc';

const LOCAL_TELEMETRY_TARGET = process.env.MISSION_CONTROL_LOCAL_TELEMETRY_URL || 'http://127.0.0.1:8765';

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5174,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8642',
        changeOrigin: true,
      },
      '/api/local': {
        target: LOCAL_TELEMETRY_TARGET,
        changeOrigin: true,
      },
    },
  },
  preview: {
    host: true,
    port: 4174,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8642',
        changeOrigin: true,
      },
      '/api/local': {
        target: LOCAL_TELEMETRY_TARGET,
        changeOrigin: true,
      },
    },
  },
});
