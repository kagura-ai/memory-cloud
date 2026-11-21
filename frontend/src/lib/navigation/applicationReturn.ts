/**
 * The link back to the application on the plan page (#2950).
 *
 * The application links to the plan page with one fixed parameter
 * (`?return=kagura-public`). The plan page then keeps a marker in this tab's
 * sessionStorage, and while it is valid shows the workspace's owner a notice
 * with a link to the one address the operator set (`KAGURA_PUBLIC_RETURN_URL`,
 * read from system info). Nothing else is read from the URL, and the marker is
 * a timestamp only: it names no user, workspace or address and never becomes
 * part of a URL.
 */

/** The in-app path of Workspace settings > Plan. */
export const PLAN_PAGE_PATH = "/workspace/settings/plan";

/**
 * The plan the notice reports, compared exactly with the workspace's plan
 * name: the server's rule for `eligible_for=kagura-public`, not "the highest
 * tier there is".
 */
export const APPLICATION_RETURN_TOP_PLAN = "promax";

const ARRIVAL_PARAM = "return";
const ARRIVAL_VALUE = "kagura-public";

/** Where a signed-out arrival is sent: sign-in, then back to the arrival URL. */
export const APPLICATION_RETURN_LOGIN_PATH =
  "/login?return_to=" +
  encodeURIComponent(`${PLAN_PAGE_PATH}?${ARRIVAL_PARAM}=${ARRIVAL_VALUE}`);

/** sessionStorage key of the marker (a `Date.now()` timestamp). */
export const APPLICATION_RETURN_STORAGE_KEY = "kagura_application_return";

/** How long after the arrival the notice is shown, in either clock direction. */
export const APPLICATION_RETURN_LIFETIME_MS = 24 * 60 * 60 * 1000;

const MARKER_PATTERN = /^\d{1,16}$/;

interface SearchParamsReader {
  getAll(name: string): string[];
}

/**
 * True only on the plan path with `return` present exactly once and equal to
 * `kagura-public`.
 */
export function isApplicationReturnArrival(
  pathname: string | null | undefined,
  params: SearchParamsReader | null | undefined,
): boolean {
  if (pathname !== PLAN_PAGE_PATH || !params) return false;
  const values = params.getAll(ARRIVAL_PARAM);
  return values.length === 1 && values[0] === ARRIVAL_VALUE;
}

/** The marker of this page load, for when sessionStorage refuses the write. */
let markerWithoutStorage: number | null = null;
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

function withinLifetime(timestamp: number): boolean {
  return Math.abs(Date.now() - timestamp) <= APPLICATION_RETURN_LIFETIME_MS;
}

/** For `useSyncExternalStore`: `remember` and `forget` notify subscribers. */
export function subscribeApplicationReturn(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Whether this tab holds a valid marker. An old or malformed value is
 * ignored, not deleted.
 */
export function hasApplicationReturn(): boolean {
  let stored: string | null = null;
  try {
    stored = window.sessionStorage.getItem(APPLICATION_RETURN_STORAGE_KEY);
  } catch {
    stored = null;
  }
  if (
    stored !== null &&
    MARKER_PATTERN.test(stored) &&
    withinLifetime(Number(stored))
  ) {
    return true;
  }
  return markerWithoutStorage !== null && withinLifetime(markerWithoutStorage);
}

/** Record an arrival now (a new arrival overwrites the previous one). */
export function rememberApplicationReturn(): void {
  const now = Date.now();
  try {
    window.sessionStorage.setItem(APPLICATION_RETURN_STORAGE_KEY, String(now));
    markerWithoutStorage = null;
  } catch {
    // Private mode or blocked site data: keep it for this page load.
    markerWithoutStorage = now;
  }
  notify();
}

/** "Hide this notice": drop the marker of this tab. */
export function forgetApplicationReturn(): void {
  markerWithoutStorage = null;
  try {
    window.sessionStorage.removeItem(APPLICATION_RETURN_STORAGE_KEY);
  } catch {
    // Unavailable storage holds no marker to remove.
  }
  notify();
}

/** Tests only: forget the page-load fallback. */
export function resetApplicationReturnFallback(): void {
  markerWithoutStorage = null;
}
