import { resolve } from 'node:path';
import { defineConfig } from 'vite';

/**
 * Self-contained bundles for the background service worker and the content
 * script.
 *
 * `ENTRY=content|worker` selects which one to build. Each is emitted as a single
 * file with every dependency inlined: an MV3 content script has no module
 * loader, so a bundle that emitted `import ... from "./chunks/x.js"` would throw
 * on injection and capture would silently never start.
 */
const entries = {
  content: {
    name: 'content-script',
    input: resolve(__dirname, 'src/content/content-script.ts'),
  },
  worker: {
    name: 'service-worker',
    input: resolve(__dirname, 'src/background/service-worker.ts'),
  },
} as const;

const selected = entries[(process.env.ENTRY as keyof typeof entries) ?? 'content'];

export default defineConfig({
  resolve: { alias: { '@': resolve(__dirname, 'src') } },
  define: { 'process.env.NODE_ENV': JSON.stringify(process.env.NODE_ENV ?? 'production') },
  publicDir: false,
  build: {
    outDir: 'dist',
    emptyOutDir: false,
    sourcemap: false,
    minify: 'esbuild',
    target: 'es2022',
    modulePreload: false,
    reportCompressedSize: false,
    lib: {
      entry: selected.input,
      formats: ['es'],
      fileName: () => `${selected.name}.js`,
    },
    rollupOptions: {
      output: { inlineDynamicImports: true },
    },
  },
});
