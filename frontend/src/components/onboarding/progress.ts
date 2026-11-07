export type PracticeStep = "context" | "creating" | "memory" | "saving" | "recall" | "ended";
export interface PracticeProgress {
  step: PracticeStep;
  contextId?: string;
  dismissed?: boolean;
}

/**
 * Change the stored progress as one step across tabs: every mutation runs
 * under the scope's Web Lock, so a dismissal in one tab and a claim in another
 * never overwrite each other. `next` gets the current progress and returns the
 * new one, or null to leave it unchanged. Web Locks exist only in secure
 * contexts; on plain HTTP the change runs directly (this tab alone).
 */
export async function updateProgress(scope: string, next: (current: PracticeProgress) => PracticeProgress | null): Promise<boolean> {
  const apply = () => {
    const progress = next(readProgress(scope));
    return progress !== null && writeProgress(scope, progress);
  };
  try {
    return navigator.locks ? await navigator.locks.request(scope, apply) : apply();
  } catch { return false; }
}

/** Atomically claim the next POST across tabs before sending it. */
export function claimPractice(scope: string, expected: "context" | "memory", pending: PracticeProgress): Promise<boolean> {
  return updateProgress(scope, current => current.step === expected && current.contextId === pending.contextId ? pending : null);
}

/** Leave the step this tab claimed; false when another tab changed it meanwhile. */
export function advancePractice(scope: string, claimed: PracticeProgress, next: PracticeProgress): Promise<boolean> {
  return updateProgress(scope, current => current.step === claimed.step && current.contextId === claimed.contextId ? next : null);
}

/** Internal name of the practice context; `crypto.randomUUID` needs a secure context. */
export function practiceContextName(): string {
  if (typeof crypto.randomUUID === "function") return `my-first-context-${crypto.randomUUID()}`;
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  return `my-first-context-${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function onboardingScope(userId: string, workspaceId: string) {
  return `onboarding:${encodeURIComponent(userId)}:${encodeURIComponent(workspaceId)}`;
}

export function readProgress(scope: string): PracticeProgress {
  try {
    const value = JSON.parse(localStorage.getItem(scope + ":wizard") ?? "null");
    if (value && ["context", "creating", "memory", "saving", "recall", "ended"].includes(value.step)) {
      return { step: value.step, contextId: typeof value.contextId === "string" ? value.contextId : undefined, dismissed: value.dismissed === true };
    }
  } catch { /* Optional onboarding must not block the workspace. */ }
  return { step: "context" };
}

function writeProgress(scope: string, progress: PracticeProgress): boolean {
  try {
    if (readProgress(scope).step === "ended" && progress.step !== "ended") return false;
    localStorage.setItem(scope + ":wizard", JSON.stringify(progress));
    return true;
  } catch {
    // Only start a practice write once its recovery marker is durable.
    return false;
  }
}

/** #2701: the screens before the practice: "prefs", then "chat", then "done". */
export type IntroStage = "prefs" | "chat" | "done";

export function readIntro(scope: string): IntroStage {
  try {
    const value = localStorage.getItem(scope + ":intro");
    if (value === "chat" || value === "done") return value;
  } catch { /* Optional onboarding must not block the workspace. */ }
  return "prefs";
}

export function writeIntro(scope: string, stage: IntroStage): void {
  try { localStorage.setItem(scope + ":intro", stage); } catch { /* Session-only fallback. */ }
}
