/**
 * Resource Tokens Configuration
 *
 * Issue #242: Centralized constants for resource token management
 *
 * #1560: the per-plan token caps that used to be mirrored here by hand now
 * come from `GET /workspaces/{id}/plan` (`quotas.max_resource_tokens` /
 * `max_quota_capacity`), and the "may create" gate from the tier matrix via
 * `usePlanFeatures`. Only the tier-independent per-token ceiling remains.
 */

/**
 * Maximum quota per individual token (events/hour)
 */
export const MAX_QUOTA_PER_TOKEN = 10000;
