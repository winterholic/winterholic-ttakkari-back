import { defineConfig } from 'vite';

// base './' 로 두어 번들을 어느 경로·별도 origin 에서 서빙해도 상대 경로로 자산을 찾는다.
export default defineConfig({
  base: './',
  build: { outDir: 'dist', chunkSizeWarningLimit: 2000 },
});
