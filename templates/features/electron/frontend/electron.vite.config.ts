import react from '@vitejs/plugin-react'
import { defineConfig } from 'electron-vite'
import { portOffset } from './src/worktreePort'

// electron-vite builds the three halves of the app into `out/`. `strictPort` plus the
// vendored `portOffset` is what lets two checkouts run `npm run dev` at once: each derives
// its own port, and neither slides onto a port serving another branch's app.
export default defineConfig({
  main: {},
  preload: {},
  renderer: {
    plugins: [react()],
    server: {
      port: Number(process.env.VITE_PORT) || 5173 + portOffset(process.cwd()),
      strictPort: true,
    },
  },
})
