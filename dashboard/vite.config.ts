import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// In dev, API calls go to the local server; in production the server serves dist/ itself.
const api = 'http://127.0.0.1:8765';
export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/signal': api, '/series': api, '/history': api, '/stats': api, '/health': api, '/portfolio': api, '/trading': api, '/research': api } },
});
