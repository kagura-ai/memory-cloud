/**
 * Kagura Icon Component (Compact)
 *
 * Simplified "KAi" logo for sidebar and small spaces.
 * Issue #51 - SVG componentization
 */

interface KaguraIconProps {
  className?: string;
}

export function KaguraIcon({ className = 'h-10 w-10' }: KaguraIconProps) {
  return (
    <svg
      xmlns="http://www.w3.workspace/2000/svg"
      viewBox="0 0 100 100"
      className={className}
    >
      {/* White background circle */}
      <circle cx="50" cy="50" r="45" fill="white" />

      {/* Ripple patterns adjusted */}
      <path
        d="M10 50 C25 40, 45 40, 60 50 S70 60, 60 65 S20 65, 10 50"
        fill="none"
        stroke="#DC2626"
        strokeWidth="0.8"
        strokeOpacity="0.25"
      />
      <path
        d="M15 45 C30 35, 40 35, 55 45 S65 55, 55 60 S25 60, 15 45"
        fill="none"
        stroke="#059669"
        strokeWidth="0.7"
        strokeOpacity="0.2"
      />
      <path
        d="M12 53 C27 58, 37 50, 47 55 C57 59, 62 50, 67 53"
        fill="none"
        stroke="#059669"
        strokeWidth="0.9"
        strokeOpacity="0.5"
      />
      <path
        d="M17 58 C32 63, 42 53, 52 58 C62 63, 67 55, 72 60"
        fill="none"
        stroke="#DC2626"
        strokeWidth="0.7"
        strokeOpacity="0.3"
      />

      {/* KAi text with gradient */}
      <defs>
        <linearGradient id="iconRedGrad" x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%" style={{ stopColor: '#DC2626', stopOpacity: 0.9 }} />
          <stop offset="100%" style={{ stopColor: '#DC2626', stopOpacity: 1 }} />
        </linearGradient>
      </defs>

      <text
        x="22"
        y="58"
        fontFamily="Palatino, 'Palatino Linotype', 'Book Antiqua', serif"
        fontSize="36"
        fontWeight="bold"
        fill="url(#iconRedGrad)"
      >
        K
      </text>
      <text
        x="48"
        y="58"
        fontFamily="Palatino, 'Palatino Linotype', 'Book Antiqua', serif"
        fontSize="36"
        fontWeight="bold"
        fill="#059669"
      >
        Ai
      </text>
    </svg>
  );
}
