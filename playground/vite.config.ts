import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/v1': { target: process.env.AXCUTOR_API_URL ?? 'http://127.0.0.1:8000', changeOrigin: true },
      '/openapi.json': { target: process.env.AXCUTOR_API_URL ?? 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
});
