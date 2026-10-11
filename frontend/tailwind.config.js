/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // 浅色为主（2026-10-11 定：不做深色）
        ink: '#1a1a1a',
        muted: '#6b7280',
        line: '#e5e7eb',
        accent: '#2563eb',
      },
    },
  },
  plugins: [],
}
