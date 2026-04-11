---
paths:
  - "frontend/**"
---

# Frontend Rules (Next.js / TypeScript)

## Architecture
- Next.js 16+ with App Router (`src/app/` directory)
- Server Components by default, `"use client"` only when needed
- TypeScript strict mode

## Styling
- Tailwind CSS for all styling
- shadcn/ui (Radix UI primitives) for accessible components
- No CSS modules or inline styles

## Data Fetching
- Client-side: `apiClient.get/post()` via `@/lib/api/base` with `useEffect` + `useState`
- Server-side: `fetch()` in Server Components
- API base URL from `NEXT_PUBLIC_API_URL` env var

## Naming
- Components: PascalCase (`MemoryCard.tsx`)
- Utilities: camelCase (`formatDate.ts`)
- Types/Interfaces: PascalCase with descriptive names

## Tabs
- In-page tabs MUST use the Tabs primitive from `@/components/ui/tabs`. Do not build ad-hoc tab UIs.
- Use `Tabs` (pill style) for facets of one entity (e.g., Overview / Settings of a context).
- Use `CategoryTabs` (underline style) for independent feature categories grouped under one route (e.g., API Keys / OAuth Apps / Resource Tokens). `CategoryTabsContent` requires a `helpText` string explaining when to use the category.
- In-page Tabs that cross information boundaries MUST sync active tab to a URL search param via `useTabParam` from `@/hooks/useTabParam`. Exception: tabs inside a Dialog/Sheet (use local state, not URL-addressable).

## Error Surface
- One channel per error class — never mix two channels for the same failure event.
- Page-level or panel-level load failures → render `<ErrorBanner error={error} />` from `@/components/common/ErrorBanner`. Do not also fire a `toast()` for the same error.
- User-action failures (save / delete / revoke triggered by a button) → `toast({ variant: "destructive", ... })`. Do not render an inline banner for the same action.
- Inline form-field validation → field-adjacent message (no toast, no banner).
- Errors raised inside a Dialog body → `<Alert variant="destructive">` inside the dialog. Toasts behind a modal are easy to miss.
- Hand-rolled red `<div>` blocks with `AlertTriangle` are a violation — replace with `ErrorBanner`. Informational gating notices (warnings, prerequisite hints) are not errors and are out of scope for this rule.

## Loading States
- Use primitives from `@/components/common/LoadingState`. Never hand-roll spinners or skeletons.
- Shape-driven selection (not author preference):
  - Table pages → `TableLoadingState rows={n}`
  - Card grids → `CardLoadingState count={n}`
  - Form / detail pages → `LoadingState lines={n}`
  - Full page gated on a single fetch → `SpinnerLoading size="lg"`
  - App bootstrap / auth gate → `PageLoading`
  - Button / row / input → `InlineSpinner`
- Prefer skeletons over spinners for first paint — skeletons preserve layout (no CLS) and communicate what is loading.
- Never leave a page blank while loading — the initial paint must render a loader, not `null`.
- Skeleton widths MUST be deterministic — never `Math.random()` in render (SSR hydration mismatch).
- Loading messages go on `SpinnerLoading` via `message` prop — skeletons are silent.
- Loading is resolved before empty — see Empty States.

## Empty States
- Distinguish empty (request succeeded, zero results) from error (request failed). They use different channels.
- New code MUST use the `EmptyState` primitive from `@/components/ui/empty-state` for any zero-item list or panel.
- Existing ad-hoc `text-center py-12` empty divs SHOULD be migrated to `EmptyState` opportunistically when already editing the file — not in a dedicated cleanup PR.
- Empty-state copy should tell the user the next action (e.g., "Create your first token") via `actionLabel` + `onAction`, not just "No items found".
- Do not use a loading spinner as a stand-in for an empty state — resolve loading first, then render empty.

## Forbidden
- No `any` type (use `unknown` or proper types)
- No `console.log` in committed code (use proper logging)
- No hardcoded API URLs (use environment variables)
- No direct DOM manipulation (use React patterns)
