/**
 * #1941: ask the capacity-lock banner to re-read the workspace usage after a
 * delete, so a cleanup clears the banner without a reload or a navigation.
 */
export const CAPACITY_REFRESH_EVENT = "kagura:capacity-refresh";

export function requestCapacityRefresh(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(CAPACITY_REFRESH_EVENT));
  }
}
