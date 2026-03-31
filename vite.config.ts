import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react-swc';

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
    },
  },
});
