import { resolve } from 'node:path';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  resolve: { alias: { '@': resolve(__dirname, 'src') } },
  test: {
    environment: 'jsdom',
    // Fixtures represent an approved ChatGPT page, so the jsdom origin matches
    // one: document.location is then correct without patching a read-only DOM
    // property, and history.replaceState can move between conversation routes.
    environmentOptions: {
      jsdom: { url: 'https://chatgpt.com/c/11111111-2222-3333-4444-555555555555' },
    },
    globals: true,
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/**/*.test.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text-summary'],
      include: ['src/modules/**', 'src/shared/**', 'src/background/state.ts'],
    },
  },
});
