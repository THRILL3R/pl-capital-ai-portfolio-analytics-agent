import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// Dev: the browser talks to Vite; /api is proxied to the local Python adapter
// (python -m portfolio_api). No CORS and no API keys in the browser.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: false } },
  },
  build: { outDir: 'dist', sourcemap: false },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
});
