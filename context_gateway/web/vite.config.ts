/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

// 배포 환경의 응답 헤더가 정본이지만, static 서빙이 헤더를 못 붙여도 최소한의 경계를 유지하려고 빌드 산출물에만 meta CSP를 넣는다.
// dev 서버는 HMR 인라인 스크립트가 필요해서 제외한다.
const csp = [
  "default-src 'none'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self' data:",
  "connect-src 'self'",
  "base-uri 'none'",
  "form-action 'none'",
  "object-src 'none'",
].join("; ");

function contentSecurityPolicy(): Plugin {
  return {
    name: "context-gateway-csp",
    apply: "build",
    transformIndexHtml: () => [{ tag: "meta", attrs: { "http-equiv": "Content-Security-Policy", content: csp }, injectTo: "head-prepend" }],
  };
}

export default defineConfig({
  // core app이 /app/ 같은 하위 경로에 mount해도 자산이 깨지지 않도록 상대 경로로 빌드한다.
  base: "./",
  plugins: [react(), contentSecurityPolicy()],
  build: { outDir: "dist", sourcemap: false, target: "es2022" },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
