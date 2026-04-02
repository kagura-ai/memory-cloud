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

## Forbidden
- No `any` type (use `unknown` or proper types)
- No `console.log` in committed code (use proper logging)
- No hardcoded API URLs (use environment variables)
- No direct DOM manipulation (use React patterns)
