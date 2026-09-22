import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { viteSingleFile } from 'vite-plugin-singlefile'

// base:'./' + vite-plugin-singlefile → 产物是单个自包含 index.html，
// 可以被 file:// 直接打开（没有 external module，不会被 Chrome 拦）。
export default defineConfig({
  plugins: [vue(), viteSingleFile()],
  base: './',
  build: {
    outDir: '../dist',
    emptyOutDir: true,
    assetsInlineLimit: 100000000,
    chunkSizeWarningLimit: 5000,
  },
})