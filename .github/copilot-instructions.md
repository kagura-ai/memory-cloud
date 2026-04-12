# Copilot Review Instructions

## Priority

- Report only high-signal issues: correctness bugs, security vulnerabilities,
  data loss risks, concurrency problems, performance regressions, broken a11y.
- Skip pure style nits unless they mask a real bug.

## Consolidation

- Group similar findings (e.g., "3 files have the same dark-mode contrast
  issue") into one comment with all locations listed, not 3 separate comments.
- If more than 5 issues found, report only the top 5 by severity. Mention the
  count of lower-priority items in a summary line.

## Project conventions

- This project uses Tailwind CSS + shadcn/ui. Do not suggest CSS modules or
  inline styles.
- Loading/empty/error states must use primitives from
  `@/components/common/LoadingState`, `@/components/ui/empty-state`,
  `@/components/common/ErrorBanner`. Flag hand-rolled alternatives.
- All user-facing strings must use `next-intl` (`useTranslations`). Flag
  hardcoded strings.
- SVG elements created via direct DOM (d3 integration) are exempt from
  "no inline styles" — SVG attributes like `fill`, `stroke`, `cursor` are
  presentation attributes, not CSS style props.

## What NOT to flag

- Test files using `as unknown as T` for defensive edge-case testing — this
  is intentional for runtime guard validation.
- Feature flag env vars that exist in Dockerfile but are not checked in code
  — these are kept for future re-gating.
- PR description accuracy for dependency lists — we update these manually
  and accept minor drift during iterative review cycles.
