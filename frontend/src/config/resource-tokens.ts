/**
 * Resource Tokens Configuration
 *
 * Issue #242: Centralized constants for resource token management
 */

import type { PlanTier } from "@/lib/utils/planLabel";

/**
 * Maximum quota per individual token (events/hour)
 */
export const MAX_QUOTA_PER_TOKEN = 10000;

/**
 * Plan-based token limits (mirrors backend `max_resource_tokens`)
 */
export const PLAN_TOKEN_LIMITS = {
  free: {
    maxTokens: 0,
    maxQuotaCapacity: 0,
  },
  basic: {
    maxTokens: 3,
    maxQuotaCapacity: 3 * MAX_QUOTA_PER_TOKEN, // 30,000
  },
  pro: {
    maxTokens: 30,
    maxQuotaCapacity: 30 * MAX_QUOTA_PER_TOKEN, // 300,000
  },
  promax: {
    maxTokens: 150,
    maxQuotaCapacity: 150 * MAX_QUOTA_PER_TOKEN, // 1,500,000
  },
} as const satisfies Record<
  PlanTier,
  { maxTokens: number; maxQuotaCapacity: number }
>;

/**
 * Calculate max quota capacity for a plan
 */
export function getMaxQuotaCapacity(planName: PlanTier): number {
  return PLAN_TOKEN_LIMITS[planName]?.maxQuotaCapacity || 0;
}

/**
 * Calculate max tokens for a plan
 */
export function getMaxTokens(planName: PlanTier): number {
  return PLAN_TOKEN_LIMITS[planName]?.maxTokens || 0;
}
