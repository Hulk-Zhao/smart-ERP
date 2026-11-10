import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Windows 下 URL.pathname 会得到 "/E:/..."，被 esbuild 拼成 "E:\E:\..."；
// 这里去掉盘符前的斜杠，得到 "E:/..."。
const fromWeb = (path: string) => {
  const resolved = new URL(path, import.meta.url).pathname;
  return /^\/[A-Za-z]:\//.test(resolved) ? resolved.slice(1) : resolved;
};

// 前端独立开发：默认走真实 /erp/api，VITE_USE_MOCK=true 时切到离线 mock
export default defineConfig({
  base: '/',
  plugins: [react()],
  resolve: {
    alias: {
      // tests/web 位于仓库根，裸模块需指回 web/node_modules（仅测试使用）。
      // 不配置时 Vite 会从 tests/web/helpers 逐级向上找不到依赖。
      '@tanstack/react-query': fromWeb('./node_modules/@tanstack/react-query'),
      'react-router-dom': fromWeb('./node_modules/react-router-dom'),
      'react-dom': fromWeb('./node_modules/react-dom'),
      react: fromWeb('./node_modules/react'),
      '@testing-library/react': fromWeb('./node_modules/@testing-library/react'),
    },
  },
  server: {
    port: 5173,
    fs: {
      // 测试位于仓库根 tests/web（S6 独占目录），允许向上读取
      allow: ['..'],
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './vitest.setup.ts',
    include: ['../tests/web/**/*.test.{ts,tsx}'],
  },
});
