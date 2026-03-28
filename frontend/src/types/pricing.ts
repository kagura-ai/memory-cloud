/**
 * Pricing page type definitions
 */

export interface PlanFeature {
  name: string;
  included: boolean;
  badge?: string;
}

export interface PricingPlan {
  name: string;
  price: string;
  period: string;
  description: string;
  gradient: string;
  popular: boolean;
  cta: string;
  savings?: string | null;
  features: {
    [category: string]: PlanFeature[];
  };
}

export interface FAQ {
  question: string;
  answer: string;
}

export interface ComparisonRow {
  name: string;
  free: string;
  basic: string;
  pro: string;
}
