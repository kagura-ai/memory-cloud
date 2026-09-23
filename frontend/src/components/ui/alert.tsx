import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils/cn";

const alertVariants = cva(
  "relative w-full rounded-lg border px-4 py-3 text-sm [&>svg+div]:translate-y-[-3px] [&>svg]:absolute [&>svg]:left-4 [&>svg]:top-4 [&>svg]:text-foreground [&>svg~*]:pl-7",
  {
    variants: {
      variant: {
        default: "bg-background text-foreground",
        // `text-destructive` maps to --destructive, which is a *fill* color
        // (used as a button background under white foreground). As Alert *text*
        // it fails WCAG AA contrast — ~3.76:1 in light and 1.78:1 in dark
        // (red-on-dark). Use the same readable red ramp as ErrorBanner so the
        // title/description meet AA in both modes (#957). Border stays on the
        // destructive token (non-text, not contrast-checked).
        destructive:
          "border-destructive/50 text-red-800 dark:border-destructive dark:text-red-300 [&>svg]:text-red-600 dark:[&>svg]:text-red-400",
        // #1646: the plan-gate (upsell) treatment. Purple is the tier colour
        // PlanBadge already uses, and the colour of the hand-rolled gate
        // notices this variant replaces — which were light-only and unreadable
        // on a dark background. Same rule as `destructive`: the colours are
        // chosen as TEXT, in both modes (purple-900 on purple-50 in light,
        // purple-100 on purple-950 in dark).
        upsell:
          "border-purple-300/70 bg-purple-50 text-purple-900 dark:border-purple-800 dark:bg-purple-950/40 dark:text-purple-100 [&>svg]:text-purple-600 dark:[&>svg]:text-purple-300",
        // #1646: the quota treatment, replacing the hand-rolled yellow / red
        // limit-reached notices. Amber text on an amber wash, dark tokens
        // included for the same reason as `upsell`.
        warning:
          "border-amber-300/70 bg-amber-50 text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100 [&>svg]:text-amber-600 dark:[&>svg]:text-amber-300",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

const Alert = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement> & VariantProps<typeof alertVariants>
>(({ className, variant, ...props }, ref) => (
  <div
    ref={ref}
    role="alert"
    className={cn(alertVariants({ variant }), className)}
    {...props}
  />
));
Alert.displayName = "Alert";

const AlertTitle = React.forwardRef<
  HTMLParagraphElement,
  React.HTMLAttributes<HTMLHeadingElement>
>(({ className, ...props }, ref) => (
  <h5
    ref={ref}
    className={cn("mb-1 font-medium leading-none tracking-tight", className)}
    {...props}
  />
));
AlertTitle.displayName = "AlertTitle";

const AlertDescription = React.forwardRef<
  HTMLParagraphElement,
  React.HTMLAttributes<HTMLParagraphElement>
>(({ className, ...props }, ref) => (
  <div
    ref={ref}
    className={cn("text-sm [&_p]:leading-relaxed", className)}
    {...props}
  />
));
AlertDescription.displayName = "AlertDescription";

export { Alert, AlertTitle, AlertDescription };
