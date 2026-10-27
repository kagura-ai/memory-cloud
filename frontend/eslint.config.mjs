import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

export default defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    // These rule families had many pre-existing hits when lint was first
    // enforced (#2202). They stay visible as warnings instead of failing CI
    // until a follow-up sweep fixes them and turns them back into errors.
    // The React Compiler rules flag effect/ref patterns whose fix can change
    // render behaviour, so each needs a reviewed change, not a mechanical one.
    rules: {
      "react-hooks/set-state-in-effect": "warn",
      "react-hooks/immutability": "warn",
      "react-hooks/refs": "warn",
      "react-hooks/static-components": "warn",
      "react-hooks/preserve-manual-memoization": "warn",
      "react-hooks/purity": "warn",
      "@typescript-eslint/no-explicit-any": "warn",
    },
  },
  {
    // .claude/rules/frontend.md forbids console.log in committed code; the
    // existing `eslint-disable-next-line no-console` comments assume this rule.
    // Warn-only for now for the same reason as above (#2202).
    rules: {
      "no-console": "warn",
    },
  },
  globalIgnores([
    ".next/**",
    "out/**",
    "build/**",
    "coverage/**",
    "playwright-report/**",
    "test-results/**",
    "next-env.d.ts",
  ]),
]);
