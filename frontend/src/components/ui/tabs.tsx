"use client";

import * as React from "react";
import * as TabsPrimitive from "@radix-ui/react-tabs";

import { cn } from "@/lib/utils/cn";

// ---------------------------------------------------------------------------
// Entity Tabs (pill style) — facets of one entity
// ---------------------------------------------------------------------------

/**
 * Entity-style tabs for switching between facets of a single entity.
 * Uses a pill/segmented style with a muted background.
 *
 * @example
 * ```tsx
 * <Tabs defaultValue="overview">
 *   <TabsList>
 *     <TabsTrigger value="overview">Overview</TabsTrigger>
 *     <TabsTrigger value="settings">Settings</TabsTrigger>
 *   </TabsList>
 *   <TabsContent value="overview">...</TabsContent>
 *   <TabsContent value="settings">...</TabsContent>
 * </Tabs>
 * ```
 */
const Tabs = TabsPrimitive.Root;

const TabsList = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.List>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.List
    ref={ref}
    className={cn(
      "inline-flex h-9 items-center justify-center rounded-lg bg-muted p-1 text-muted-foreground",
      className,
    )}
    {...props}
  />
));
TabsList.displayName = TabsPrimitive.List.displayName;

const TabsTrigger = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.Trigger
    ref={ref}
    className={cn(
      "inline-flex items-center justify-center whitespace-nowrap rounded-md px-3 py-1 text-sm font-medium ring-offset-background transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow",
      className,
    )}
    {...props}
  />
));
TabsTrigger.displayName = TabsPrimitive.Trigger.displayName;

const TabsContent = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Content>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.Content
    ref={ref}
    className={cn(
      "mt-2 ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
      className,
    )}
    {...props}
  />
));
TabsContent.displayName = TabsPrimitive.Content.displayName;

// ---------------------------------------------------------------------------
// Category Tabs (underline style) — independent feature categories
// ---------------------------------------------------------------------------

/**
 * Category-style tabs for switching between independent feature surfaces
 * grouped under one route. Uses an underline style with no background.
 * `CategoryTabsContent` requires a `helpText` prop explaining when to use
 * the selected category.
 *
 * @example
 * ```tsx
 * <CategoryTabs defaultValue="api-keys">
 *   <CategoryTabsList>
 *     <CategoryTabsTrigger value="api-keys">API Keys</CategoryTabsTrigger>
 *     <CategoryTabsTrigger value="oauth">OAuth Apps</CategoryTabsTrigger>
 *   </CategoryTabsList>
 *   <CategoryTabsContent value="api-keys" helpText="Use API keys for server-to-server authentication.">
 *     ...
 *   </CategoryTabsContent>
 *   <CategoryTabsContent value="oauth" helpText="Use OAuth apps for user-facing integrations.">
 *     ...
 *   </CategoryTabsContent>
 * </CategoryTabs>
 * ```
 */
const CategoryTabs = TabsPrimitive.Root;
CategoryTabs.displayName = "CategoryTabs";

const CategoryTabsList = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.List>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.List
    ref={ref}
    className={cn(
      "inline-flex h-10 items-center gap-4 border-b border-border text-muted-foreground",
      className,
    )}
    {...props}
  />
));
CategoryTabsList.displayName = "CategoryTabsList";

const CategoryTabsTrigger = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.Trigger
    ref={ref}
    className={cn(
      "inline-flex items-center justify-center whitespace-nowrap px-1 py-2 text-sm font-medium ring-offset-background transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 border-b-2 border-transparent -mb-px data-[state=active]:border-primary data-[state=active]:text-foreground",
      className,
    )}
    {...props}
  />
));
CategoryTabsTrigger.displayName = "CategoryTabsTrigger";

interface CategoryTabsContentProps extends React.ComponentPropsWithoutRef<
  typeof TabsPrimitive.Content
> {
  helpText: string;
}

const CategoryTabsContent = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Content>,
  CategoryTabsContentProps
>(({ className, helpText, children, ...props }, ref) => {
  const descId = `category-tab-desc-${props.value}`;
  return (
    <TabsPrimitive.Content
      ref={ref}
      aria-describedby={helpText ? descId : undefined}
      className={cn(
        "mt-4 ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
        className,
      )}
      {...props}
    >
      {helpText && (
        <p id={descId} className="text-sm text-muted-foreground mb-4">
          {helpText}
        </p>
      )}
      {children}
    </TabsPrimitive.Content>
  );
});
CategoryTabsContent.displayName = "CategoryTabsContent";

export {
  Tabs,
  TabsList,
  TabsTrigger,
  TabsContent,
  CategoryTabs,
  CategoryTabsList,
  CategoryTabsTrigger,
  CategoryTabsContent,
};
