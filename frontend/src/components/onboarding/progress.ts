export type PracticeStep = "context" | "creating" | "memory" | "saving" | "recall" | "ended";
export interface PracticeProgress {
  step: PracticeStep;
  contextId?: string;
  dismissed?: boolean;
}

/** Atomically claim the next POST across tabs before sending it. */
export async function claimPractice(scope: string, expected: "context" | "memory", pending: PracticeProgress): Promise<boolean> {
  if (!navigator.locks) return false;
  try {
    return await navigator.locks.request(scope, () => {
      const previous = readProgress(scope);
      return previous.step === expected && previous.contextId === pending.contextId && writeProgress(scope, pending);
    });
  } catch { return false; }
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

export function writeProgress(scope: string, progress: PracticeProgress): boolean {
  try {
    if (readProgress(scope).step === "ended" && progress.step !== "ended") return false;
    localStorage.setItem(scope + ":wizard", JSON.stringify(progress));
    return true;
  } catch {
    // Only start a practice write once its recovery marker is durable.
    return false;
  }
}
