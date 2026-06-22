/**
 * Kagura Logo Component — flat 5-colour brand (issue #1029).
 *
 * Replaces the legacy Palatino wordmark (#51). A single self-contained,
 * theme-independent SVG: the 5-petal pinwheel mark (vectorized from the
 * kagura-www brand assets, commit 35bbf7f) plus the "Kagura AI" wordmark.
 * One SVG so it scales cleanly at every call site (sidebar h-8 → auth hero
 * h-24) without layout fiddling. The mark's petal colours are inlined hex (a
 * brand mark must not shift with the app theme), but the "Kagura" wordmark uses
 * `currentColor` so it stays legible on dark backgrounds (e.g. the dashboard
 * sidebar) — "AI" keeps the ukon accent on every theme.
 */

interface KaguraLogoProps {
  className?: string;
  /**
   * "full" (default) = SVG mark + SVG wordmark text (theme-adaptive via
   * currentColor — use on dark surfaces like the sidebar). "mark" = pinwheel
   * only. "image" = the official brand PNGs (mark + wordmark) embedded via
   * SVG <image> — pixel-perfect typography. Pair with `surface` to pick the
   * wordmark colour: "light" surface → dark wordmark (auth pages), "dark"
   * surface → white wordmark (e.g. the dashboard sidebar).
   */
  variant?: "full" | "mark" | "image";
  /** For variant="image": tone of the surface behind the lockup. Default "light". */
  surface?: "light" | "dark";
}

const PETALS = [
  { cx: 100, cy: 42, grad: "kg-tokiwa" }, // top — evergreen
  { cx: 155, cy: 82, grad: "kg-shu" }, // right — vermilion
  { cx: 134, cy: 147, grad: "kg-ukon" }, // bottom-right — gold
  { cx: 66, cy: 147, grad: "kg-gofun" }, // bottom-left — pale
  { cx: 45, cy: 82, grad: "kg-kodai" }, // left — purple
] as const;

// White "pinwheel" notch per petal: a head dot near the inner rim plus a
// short stem pointing at the centre (100,100). Pre-computed so the mark stays
// declarative.
const NOTCHES = [
  { hx: 100, hy: 58, sx: 100, sy: 82 },
  { hx: 139.8, hy: 87, sx: 117, sy: 94.4 },
  { hx: 124.6, hy: 134, sx: 110.6, sy: 114.6 },
  { hx: 75.4, hy: 134, sx: 89.4, sy: 114.6 },
  { hx: 60.2, hy: 87, sx: 83, sy: 94.4 },
] as const;

function MarkDefs() {
  return (
    <defs>
      <linearGradient id="kg-tokiwa" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stopColor="#3f9168" />
        <stop offset="1" stopColor="#00664b" />
      </linearGradient>
      <linearGradient id="kg-shu" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stopColor="#f3893a" />
        <stop offset="1" stopColor="#eb6100" />
      </linearGradient>
      <linearGradient id="kg-ukon" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stopColor="#fdc659" />
        <stop offset="1" stopColor="#faa916" />
      </linearGradient>
      <linearGradient id="kg-gofun" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stopColor="#f2f1cf" />
        <stop offset="1" stopColor="#dcdf9a" />
      </linearGradient>
      <linearGradient id="kg-kodai" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stopColor="#9c79ae" />
        <stop offset="1" stopColor="#70477c" />
      </linearGradient>
    </defs>
  );
}

function MarkBody() {
  return (
    <>
      {PETALS.map((p) => (
        <circle
          key={p.grad}
          cx={p.cx}
          cy={p.cy}
          r="38"
          fill={`url(#${p.grad})`}
        />
      ))}
      <g stroke="#fffffb" strokeWidth="9" strokeLinecap="round" fill="#fffffb">
        {NOTCHES.map((n) => (
          <line
            key={`s-${n.hx}-${n.hy}`}
            x1={n.hx}
            y1={n.hy}
            x2={n.sx}
            y2={n.sy}
          />
        ))}
        {NOTCHES.map((n) => (
          <circle
            key={`h-${n.hx}-${n.hy}`}
            cx={n.hx}
            cy={n.hy}
            r="8"
            stroke="none"
          />
        ))}
      </g>
    </>
  );
}

export function KaguraLogo({
  className = "h-10 w-auto",
  variant = "full",
  surface = "light",
}: KaguraLogoProps) {
  if (variant === "mark") {
    return (
      <svg
        xmlns="http://www.w3.org/2000/svg"
        viewBox="0 0 200 200"
        className={className}
        role="img"
        aria-label="Kagura"
      >
        <MarkDefs />
        <MarkBody />
      </svg>
    );
  }

  if (variant === "image") {
    // Official brand PNGs (transparent) embedded via SVG <image> so the lockup
    // scales with `className` and avoids the next/image <img> lint rule. On dark
    // surfaces (e.g. the sidebar) the black wordmark is swapped for a white one;
    // the mark PNG is theme-independent (transparent pinwheel) so it needs none.
    const wordmark =
      surface === "dark"
        ? "/brand/kagura-wordmark-light.png"
        : "/brand/kagura-wordmark.png";
    return (
      <svg
        xmlns="http://www.w3.org/2000/svg"
        viewBox="0 0 560 120"
        className={className}
        role="img"
        aria-label="Kagura AI"
      >
        <image
          href="/brand/kagura-mark.png"
          x="6"
          y="8"
          width="108"
          height="104"
        />
        <image href={wordmark} x="146" y="23" width="408" height="102" />
      </svg>
    );
  }

  // Full horizontal lockup: mark on the left, "Kagura AI" wordmark on the right.
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 440 120"
      className={className}
      role="img"
      aria-label="Kagura AI"
    >
      <MarkDefs />
      <g transform="translate(12 10) scale(0.5)">
        <MarkBody />
      </g>
      <text
        x="132"
        y="78"
        fontFamily="'Noto Sans JP', system-ui, -apple-system, 'Segoe UI', sans-serif"
        fontSize="56"
        fontWeight="700"
        letterSpacing="-1.5"
      >
        <tspan fill="currentColor">Kagura</tspan>
        <tspan fill="#faa916"> AI</tspan>
      </text>
    </svg>
  );
}
