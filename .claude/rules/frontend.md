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

## Forbidden
- No `any` type (use `unknown` or proper types)
- No `console.log` in committed code (use proper logging)
- No hardcoded API URLs (use environment variables)
- No direct DOM manipulation (use React patterns)
