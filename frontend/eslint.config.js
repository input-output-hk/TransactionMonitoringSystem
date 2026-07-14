import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      // Type-aware ruleset: catches misused promises, unsafe `any` flows and
      // wrong await targets that the syntax-only preset cannot see.
      tseslint.configs.recommendedTypeChecked,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
      parserOptions: {
        // projectService resolves each file against the solution-style
        // tsconfig (tsconfig.app.json for src/, tsconfig.node.json for
        // vite.config.ts) without pinning a single project path here.
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      // Debug console.log/.debug/.info must not ship to production; intentional
      // diagnostics use console.warn / console.error (allowed).
      'no-console': ['error', { allow: ['warn', 'error'] }],
    },
  },
  {
    // shadcn/ui primitives re-export Radix components (`const X = Primitive.Root`),
    // which the Fast Refresh heuristic can't recognize as components. These are
    // vendored UI building blocks; HMR granularity here isn't worth splitting
    // every file, so the dev-only rule is disabled for the ui/ folder.
    files: ['src/components/ui/**/*.{ts,tsx}'],
    rules: {
      'react-refresh/only-export-components': 'off',
    },
  },
])
