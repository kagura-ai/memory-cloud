/**
 * Kagura Logo with White Background (Large)
 *
 * Full logo with white background for hero sections.
 * Issue #51 - SVG componentization
 */

interface KaguraLogoBgWhiteProps {
  className?: string;
}

export function KaguraLogoBgWhite({ className = 'h-20 w-auto' }: KaguraLogoBgWhiteProps) {
  return (
    <svg
      xmlns="http://www.w3.workspace/2000/svg"
      viewBox="0 0 300 100"
      className={className}
    >
      {/* White background */}
      <rect width="300" height="100" fill="white" />

      {/* Irregular ripples */}
      <path
        d="M70 40 C110 15, 190 25, 230 40 S280 65, 250 70 S170 80, 150 70 S90 65, 70 40"
        fill="none"
        stroke="#DC2626"
        strokeWidth="0.8"
        strokeOpacity="0.25"
      />
      <path
        d="M75 35 C125 20, 175 10, 225 35 S265 45, 245 65 S185 75, 145 65 S95 50, 75 35"
        fill="none"
        stroke="#059669"
        strokeWidth="0.7"
        strokeOpacity="0.2"
      />
      <path
        d="M85 30 C135 5, 165 15, 215 25 S265 45, 235 55 S155 65, 135 55 S95 45, 85 30"
        fill="none"
        stroke="#DC2626"
        strokeWidth="0.6"
        strokeOpacity="0.15"
      />
      <path
        d="M65 45 C115 25, 185 30, 235 45 S275 70, 255 75 S175 85, 155 75 S85 70, 65 45"
        fill="none"
        stroke="#059669"
        strokeWidth="0.5"
        strokeOpacity="0.1"
      />

      {/* Kagura text */}
      <defs>
        <linearGradient id="bgWhiteRedGrad" x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%" style={{ stopColor: '#DC2626', stopOpacity: 0.9 }} />
          <stop offset="100%" style={{ stopColor: '#DC2626', stopOpacity: 1 }} />
        </linearGradient>
      </defs>
      <text
        x="150"
        y="45"
        fontFamily="Palatino, 'Palatino Linotype', 'Book Antiqua', serif"
        fontSize="36"
        fontWeight="bold"
        fill="url(#bgWhiteRedGrad)"
        textAnchor="middle"
      >
        Kagura
      </text>

      {/* Ai text */}
      <text
        x="150"
        y="80"
        fontFamily="Palatino, 'Palatino Linotype', 'Book Antiqua', serif"
        fontSize="32"
        fontWeight="bold"
        fill="#059669"
        textAnchor="middle"
      >
        Ai
      </text>

      {/* Asymmetric waves */}
      <path
        d="M75 48 C100 53, 130 45, 160 50 C190 54, 210 45, 235 48"
        fill="none"
        stroke="#059669"
        strokeWidth="0.9"
        strokeOpacity="0.5"
      />
      <path
        d="M80 53 C110 58, 150 48, 180 53 C210 58, 220 50, 240 55"
        fill="none"
        stroke="#DC2626"
        strokeWidth="0.7"
        strokeOpacity="0.3"
      />
    </svg>
  );
}
