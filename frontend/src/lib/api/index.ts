/**
 * API Module Barrel Export
 * Issue #59 - /lib directory reorganization
 */

// Base API client
export { apiClient, ApiError } from "./base";

// Domain-specific APIs
export * from "./memory";
export * from "./api-keys";
export * from "./coding-sessions";
export * from "./doctor";
export * from "./oauth";
export * from "./external-keys";
export * from "./graph";
export * from "./system";
