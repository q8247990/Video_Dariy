import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) {
            return undefined
          }

          if (id.includes('react-player') || id.includes('hls.js')) {
            return 'vendor-media'
          }
          if (id.includes('@tanstack/react-query')) {
            return 'vendor-query'
          }
          if (id.includes('react') || id.includes('react-dom')) {
            return 'vendor-react'
          }
          if (id.includes('lucide-react')) {
            return 'vendor-icons'
          }
          return 'vendor'
        },
      },
    },
  },
})
