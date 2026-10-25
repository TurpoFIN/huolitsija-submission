// Library build: one ESM file with MapLibre and deck.gl bundled in, React external.
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2020',
    sourcemap: true,
    cssCodeSplit: false,
    chunkSizeWarningLimit: 3000,
    lib: { entry: 'src/bundle.ts', formats: ['es'], fileName: () => 'huolitsija-shipment-map.js', cssFileName: 'style' },
    rollupOptions: { external: ['react', 'react-dom', 'react/jsx-runtime', 'react-dom/client'] },
  },
});
