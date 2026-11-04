export type PracticeStep = "context" | "creating" | "memory" | "saving" | "recall";
export interface PracticeProgress {
  step: PracticeStep;
  contextId?: string;
  dismissed?: boolean;
}

export function onboardingScope(userId: string, workspaceId: string) {
  return `onboarding:${encodeURIComponent(userId)}:${encodeURIComponent(workspaceId)}`;
}

export function readProgress(scope: string): PracticeProgress {
  try {
    const value = JSON.parse(localStorage.getItem(scope + ":wizard") ?? "null");
    if (value && ["context", "creating", "memory", "saving", "recall"].includes(value.step)) {
      return { step: value.step, contextId: typeof value.contextId === "string" ? value.contextId : undefined, dismissed: value.dismissed === true };
    }
  } catch { /* Optional onboarding must not block the workspace. */ }
  return { step: "context" };
}

export function writeProgress(scope: string, progress: PracticeProgress): boolean {
  try {
    localStorage.setItem(scope + ":wizard", JSON.stringify(progress));
    return true;
  } catch {
    // Only start a practice write once its recovery marker is durable.
    return false;
  }
}
