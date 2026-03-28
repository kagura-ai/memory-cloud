/**
 * Design Tokens
 *
 * Unified design system for Kagura Memory Cloud frontend.
 * Ensures consistency across all pages and components.
 * Issue #31: Frontend Redesign Phase 5
 */

// ============================================================================
// Spacing
// ============================================================================

export const spacing = {
  // Page-level spacing
  page: 'p-6',
  pageY: 'py-6',
  pageX: 'px-6',

  // Container spacing
  container: 'max-w-7xl mx-auto',

  // Section spacing
  section: 'space-y-6',
  sectionMargin: 'mb-8',

  // Card spacing
  card: 'p-6',
  cardCompact: 'p-4',
  cardLarge: 'p-8',

  // Element spacing
  gap: {
    xs: 'gap-2',
    sm: 'gap-3',
    md: 'gap-4',
    lg: 'gap-6',
    xl: 'gap-8',
  },

  // Stack spacing
  stack: {
    xs: 'space-y-2',
    sm: 'space-y-3',
    md: 'space-y-4',
    lg: 'space-y-6',
    xl: 'space-y-8',
  },
};

// ============================================================================
// Typography
// ============================================================================

export const typography = {
  // Headings
  h1: 'text-3xl font-bold tracking-tight text-slate-900 dark:text-white',
  h2: 'text-2xl font-semibold tracking-tight text-slate-900 dark:text-white',
  h3: 'text-xl font-semibold text-slate-900 dark:text-white',
  h4: 'text-lg font-medium text-slate-900 dark:text-white',

  // Body text
  body: 'text-base text-slate-700 dark:text-slate-300',
  bodyLarge: 'text-lg text-slate-700 dark:text-slate-300',
  bodySmall: 'text-sm text-slate-600 dark:text-slate-400',

  // Label
  label: 'text-sm font-medium text-slate-700 dark:text-slate-300',

  // Caption
  caption: 'text-xs text-slate-500 dark:text-slate-400',

  // Description
  description: 'text-sm text-slate-600 dark:text-slate-400',
  descriptionLarge: 'text-base text-slate-600 dark:text-slate-400',

  // Code
  code: 'font-mono text-sm',

  // Link
  link: 'text-brand-green-600 hover:text-brand-green-700 dark:text-brand-green-400 dark:hover:text-brand-green-300 underline-offset-4 hover:underline',
};

// ============================================================================
// Colors
// ============================================================================

export const colors = {
  // Button colors
  button: {
    primary: 'bg-brand-green-600 hover:bg-brand-green-700 text-white focus:ring-brand-green-500',
    secondary: 'bg-slate-600 hover:bg-slate-700 text-white focus:ring-slate-500',
    outline: 'border-2 border-slate-300 hover:bg-slate-50 dark:border-slate-600 dark:hover:bg-slate-800',
    ghost: 'hover:bg-slate-100 dark:hover:bg-slate-800',
    danger: 'bg-red-600 hover:bg-red-700 text-white focus:ring-red-500',
    success: 'bg-green-600 hover:bg-green-700 text-white focus:ring-green-500',
  },

  // Badge colors
  badge: {
    default: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
    primary: 'bg-brand-green-100 text-brand-green-700 dark:bg-brand-green-900 dark:text-brand-green-300',
    success: 'bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300',
    warning: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900 dark:text-yellow-300',
    danger: 'bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-300',
    info: 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300',
  },

  // Alert colors
  alert: {
    success: 'bg-green-50 border-green-200 text-green-800 dark:bg-green-900/20 dark:border-green-800 dark:text-green-300',
    warning: 'bg-yellow-50 border-yellow-200 text-yellow-800 dark:bg-yellow-900/20 dark:border-yellow-800 dark:text-yellow-300',
    danger: 'bg-red-50 border-red-200 text-red-800 dark:bg-red-900/20 dark:border-red-800 dark:text-red-300',
    info: 'bg-blue-50 border-blue-200 text-blue-800 dark:bg-blue-900/20 dark:border-blue-800 dark:text-blue-300',
  },

  // Background colors
  bg: {
    page: 'bg-slate-50 dark:bg-slate-900',
    card: 'bg-white dark:bg-slate-950',
    hover: 'hover:bg-slate-50 dark:hover:bg-slate-900',
    selected: 'bg-slate-100 dark:bg-slate-800',
  },

  // Border colors
  border: {
    default: 'border-slate-200 dark:border-slate-800',
    focus: 'focus:border-brand-green-500 focus:ring-brand-green-500',
  },

  // Text colors
  text: {
    primary: 'text-slate-900 dark:text-white',
    secondary: 'text-slate-600 dark:text-slate-400',
    muted: 'text-slate-500 dark:text-slate-500',
    accent: 'text-brand-green-600 dark:text-brand-green-400',
    danger: 'text-red-600 dark:text-red-400',
    success: 'text-green-600 dark:text-green-400',
  },
};

// ============================================================================
// Shadows
// ============================================================================

export const shadows = {
  sm: 'shadow-sm',
  md: 'shadow-md',
  lg: 'shadow-lg',
  xl: 'shadow-xl',
  card: 'shadow-sm hover:shadow-md transition-shadow',
};

// ============================================================================
// Borders
// ============================================================================

export const borders = {
  default: 'border border-slate-200 dark:border-slate-800',
  thick: 'border-2 border-slate-200 dark:border-slate-800',
  rounded: {
    sm: 'rounded-sm',
    md: 'rounded-md',
    lg: 'rounded-lg',
    xl: 'rounded-xl',
    '2xl': 'rounded-2xl',
    full: 'rounded-full',
  },
};

// ============================================================================
// Transitions
// ============================================================================

export const transitions = {
  default: 'transition-colors duration-200',
  all: 'transition-all duration-200',
  fast: 'transition-all duration-150',
  slow: 'transition-all duration-300',
};

// ============================================================================
// Loading States
// ============================================================================

export const loading = {
  // Spinner sizes
  spinner: {
    xs: 'h-3 w-3 border-2',
    sm: 'h-4 w-4 border-2',
    md: 'h-8 w-8 border-3',
    lg: 'h-12 w-12 border-4',
    xl: 'h-16 w-16 border-4',
  },

  // Spinner colors
  colors: {
    default: 'border-slate-200 dark:border-slate-700 border-t-slate-600 dark:border-t-slate-400',
    brand: 'border-brand-green-200 dark:border-brand-green-900 border-t-brand-green-600',
  },

  // Animations
  animations: {
    spin: 'animate-spin',
    pulse: 'animate-pulse',
  },
};

// ============================================================================
// Layout
// ============================================================================

export const layout = {
  // Flex
  flexRow: 'flex flex-row items-center',
  flexCol: 'flex flex-col',
  flexCenter: 'flex items-center justify-center',
  flexBetween: 'flex items-center justify-between',

  // Grid
  grid: {
    cols2: 'grid grid-cols-1 md:grid-cols-2',
    cols3: 'grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3',
    cols4: 'grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4',
  },

  // Container
  container: 'container mx-auto px-6',
  containerFluid: 'w-full px-6',

  // Card
  card: 'bg-white dark:bg-slate-950 border border-slate-200 dark:border-slate-800 rounded-lg shadow-sm',
};

// ============================================================================
// Responsive
// ============================================================================

export const responsive = {
  // Breakpoints (Tailwind defaults)
  // sm: 640px
  // md: 768px
  // lg: 1024px
  // xl: 1280px
  // 2xl: 1536px

  // Hide/Show
  hideMobile: 'hidden md:block',
  hideDesktop: 'block md:hidden',

  // Sidebar
  sidebarWidth: 'w-64',
  sidebarCollapsed: 'w-20',
};

// ============================================================================
// Helper: Combine classes
// ============================================================================

/**
 * Utility function to combine design token classes
 * Usage: cn(typography.h1, spacing.section)
 */
export function cn(...classes: (string | undefined | null | false)[]): string {
  return classes.filter(Boolean).join(' ');
}
