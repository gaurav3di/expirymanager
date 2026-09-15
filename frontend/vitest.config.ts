// The test runner's configuration, kept separate from vite.config.ts on purpose.
//
// Vitest reads this file INSTEAD of vite.config.ts when both exist. It does not merge the two, so
// everything a test needs in order to resolve has to be repeated here: the @ alias and the React
// transform. Two tests import .tsx components and render them to static markup, and they resolve
// nothing without both.
//
// What is deliberately NOT repeated is the dev server block. That block reads a TLS key pair off
// disk and proxies /api to the running application on 127.0.0.1:8000. A test run must never open
// a socket to that process: the suite has to answer the same on a machine where the app has never
// run, and a test that silently reached a live, logged-in backend would report whatever that
// machine happened to hold rather than what the code does. Every suite here stubs fetch.

import path from 'node:path'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react()],
  resolve: {
    // Same alias as vite.config.ts, and the same spelling of it. import.meta.dirname rather than
    // __dirname: this config is ESM.
    alias: { '@': path.resolve(import.meta.dirname, './src') },
  },
  test: {
    // Node by default, and jsdom per file through the `// @vitest-environment jsdom` pragma, so a
    // pure module test is not charged for a DOM it never touches. Roughly two thirds of the files
    // here need no DOM at all.
    environment: 'node',
    // describe, it and expect are imported by name in every file. Leaving them off the global
    // object keeps an undeclared helper a failure rather than an accidental pass.
    globals: false,
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    // node_modules holds openalgo-charts, which ships its own tests in source form.
    exclude: ['node_modules/**', 'dist/**'],
  },
})
