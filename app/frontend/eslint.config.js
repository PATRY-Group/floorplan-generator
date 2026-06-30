// Flat ESLint config (ESLint 9). Focus: catch real bugs and the issues this
// codebase has had — hooks misuse / stale deps (react-hooks), accessibility gaps
// on interactive elements (jsx-a11y), and missing list keys (react). Run with
// `npm run lint`. Not wired into CI yet — it surfaces warnings on existing code
// to clean up incrementally rather than block the build.
//
// Only the two classic react-hooks rules are enabled (rules-of-hooks +
// exhaustive-deps); react-hooks 7.x also ships aggressive React-Compiler rules
// (immutability/purity/etc.) that don't fit this hand-optimised code yet.
import js from "@eslint/js";
import globals from "globals";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import jsxA11y from "eslint-plugin-jsx-a11y";

export default [
  { ignores: ["dist/**", "node_modules/**"] },
  js.configs.recommended,
  react.configs.flat.recommended,
  jsxA11y.flatConfigs.recommended,
  {
    files: ["src/**/*.{js,jsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: { ...globals.browser },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    settings: { react: { version: "detect" } },
    plugins: { "react-hooks": reactHooks },
    rules: {
      // real bugs stay errors
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "no-unused-vars": ["error", {
        argsIgnorePattern: "^_",      // intentional positional placeholders, e.g. (_, j) =>
        varsIgnorePattern: "^_",
        caughtErrors: "none",         // `catch (e) { /* ignore */ }` is a deliberate pattern here
      }],
      "react/react-in-jsx-scope": "off",  // Vite/React 18 JSX transform — no React import needed
      "react/prop-types": "off",          // this codebase doesn't use prop-types
      // a11y: advisory for this internal tool (deliberate modal-backdrop / wrapped-label
      // patterns). Visible for incremental cleanup, but not build-breaking.
      "react/no-unescaped-entities": "off",
      "jsx-a11y/label-has-associated-control": "warn",
      "jsx-a11y/click-events-have-key-events": "warn",
      "jsx-a11y/no-static-element-interactions": "warn",
      "jsx-a11y/no-noninteractive-element-interactions": "warn",
      "jsx-a11y/no-noninteractive-tabindex": "warn",
      "jsx-a11y/no-autofocus": "warn",
    },
  },
];
