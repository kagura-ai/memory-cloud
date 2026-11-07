"use client";

/**
 * First-run onboarding flow (Issue #952).
 *
 * Guides a brand-new user to a "value moment" in ~5 minutes: create a first
 * context, save a sample memory, and recall it — all in-app — then points them
 * at MCP for ongoing memory creation (the real workflow; the web app is a
 * management UI, memories are normally written by AI clients via MCP tools).
 *
 * Design decisions (gate1 green, PM-reviewed — see issue #952):
 *  - Trigger: workspace owner with no contexts, or a verified practice checkpoint
 *    (localStorage). No server-side "seen" flag — re-showing on a fresh device
 *    or after deleting all contexts is acceptable for 1.0.
 *  - Hybrid value moment: a minimal in-app remember()/recall() makes the first
 *    recall return a recognizable hit, WITHOUT pulling in the #951 importer.
 *  - recall-vs-explore explainer is COPY ONLY: recall does NOT auto-trigger
 *    explore (mixing graph signals into recall degrades precision, #120/#216).
 *  - Dismissible modal on the dashboard, outside the authentication flow.
 *  - The `?context=<id>` navigation (URL-driven current context, #246) is
 *    deferred to the final "Finish" CTA: navigating mid-flow would remount this
 *    card and (with 1 context now) hide it, killing the stepper.
 *  - remember/recall need available embeddings; if absent we show a
 *    non-error "set up embeddings" notice instead of a guaranteed-to-fail flow.
 *  - MCP setup link (#2698): shown from the first step and on the done step,
 *    pointing at the connection card on the Integrations entry. Clients
 *    register over OAuth, so a new user needs no API key to connect.
 */

import Link from "next/link";
import { useState, useEffect, useCallback, useRef } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { advancePractice, claimPractice, onboardingScope, practiceContextName, readProgress, updateProgress } from "./progress";
import { ApiError } from "@/lib/api/base";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import {
  Sparkles,
  ArrowRight,
  Check,
  Search,
  KeyRound,
  ExternalLink,
} from "lucide-react";

import { useWorkspace } from "@/contexts/WorkspaceContext";
import { getContexts, createContext } from "@/lib/api/contexts";
import { rememberMemory, recallMemories } from "@/lib/api/memory";
import { checkOpenAIKeyStatus } from "@/lib/api/workspaces";
import { useSystemFeatures } from "@/hooks/useSystemFeatures";
import type { Context } from "@/lib/types/context";
import type { RecallResultItem } from "@/lib/types/memory";

import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { InlineSpinner } from "@/components/common/LoadingState";

type Step = "context" | "memory" | "recall" | "done" | "recovery";

const MCP_CONNECTION_HREF = "/workspace/integrations#mcp-connection";

interface PracticeOptions {
  replay?: boolean;
  onClose?: () => void;
  onCreated?: (context: Context) => void;
}

export function OnboardingCard({ replay = false, onClose, onCreated }: PracticeOptions) {
  const { user } = useAuth();
  const { currentWorkspace, loading } = useWorkspace();
  // Only owners can create a private practice context. Others use the read-only guide.
  if (!user?.id || loading || !currentWorkspace || currentWorkspace.current_user_role !== "owner") return null;
  const scope = onboardingScope(user.id, currentWorkspace.id);
  return <PracticeCard key={scope} scope={scope} userId={user.id} replay={replay} onClose={onClose} onCreated={onCreated} />;
}

function PracticeCard({ scope, userId, replay, onClose, onCreated }: PracticeOptions & { scope: string; userId: string }) {
  const t = useTranslations("onboarding");
  const router = useRouter();
  const {
    currentWorkspace,
    currentWorkspaceId,
    loading: workspaceLoading,
  } = useWorkspace();

  // `ready` gates the first paint: we render nothing until the mount-time
  // checks (dismissed? role? zero contexts? has key?) resolve, to avoid a
  // flash of onboarding for users who shouldn't see it.
  const [ready, setReady] = useState(false);
  const [visible, setVisible] = useState(false);
  const [hasKey, setHasKey] = useState(true);

  const [step, setStep] = useState<Step>("context");
  const [contextId, setContextId] = useState<string | null>(null);
  // Seed the editable sample copy from i18n ONCE via lazy initializers — never
  // in an effect keyed on `t`, which would clobber the user's edits whenever the
  // `t` identity changes (locale switch / provider re-render).
  const [summary, setSummary] = useState(() => t("memory.sampleSummary"));
  const [content, setContent] = useState(() => t("memory.sampleContent"));
  const [query, setQuery] = useState(() => t("recall.sampleQuery"));
  const [results, setResults] = useState<RecallResultItem[]>([]);
  const [busy, setBusy] = useState(false);
  // A pending create or save keeps the guide open (a second write must not
  // start from a closed and replayed guide); a read-only Recall does not.
  const [writePending, setWritePending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const writing = useRef(false);

  // Issue #1167: with BYOK off the key-status probe 404s and env keys serve
  // embeddings — skip the probe (hasKey keeps its default true → normal
  // onboarding, no "configure key" CTA pointing at a disabled page).
  const systemFeatures = useSystemFeatures();
  const byokEnabled = systemFeatures?.byok === true;

  useEffect(() => {
    let alive = true;
    // Wait for the workspace to hydrate — current_user_role is null mid-load.
    // Also wait for the feature flags (#1167) so the byok gate below reads a
    // resolved value instead of skipping the probe on the loading state.
    if (workspaceLoading || !currentWorkspace || !currentWorkspaceId) return;
    if (systemFeatures === null) return;

    const saved = readProgress(scope);
    if (saved.step === "ended" || (saved.dismissed && !replay)) {
      setReady(true);
      return;
    }
    (async () => {
      try {
        const { contexts } = await getContexts();
        if (!alive) return;
        // The server lists only this user's accessible contexts in the selected workspace.
        const sample = contexts.find(ctx => ctx.id === saved.contextId && ctx.is_private && !ctx.is_public && ctx.workspace_id === currentWorkspaceId && ctx.created_by === userId);
        if (saved.step === "creating" || saved.step === "saving" || (saved.contextId && !sample)) {
          setStep("recovery");
          setVisible(true);
          return;
        }
        if (contexts.length > 0 && !sample) return;
        if (sample) {
          setContextId(sample.id);
          setStep(saved.step === "recall" ? "recall" : "memory");
        }
        const status = byokEnabled ? await checkOpenAIKeyStatus(currentWorkspaceId).catch(() => null) : null;
        if (!alive) return;
        if (status) setHasKey(status.embedding_available !== false);
        setVisible(true);
      } catch {
        // Best-effort: a failed probe just leaves the card hidden rather than
        // risking a broken onboarding over the user's real dashboard.
      } finally {
        if (alive) setReady(true);
      }
    })();

    return () => {
      alive = false;
    };
    // byokEnabled is derived from systemFeatures, so listing systemFeatures
    // alone covers both.
  }, [workspaceLoading, currentWorkspace, currentWorkspaceId, systemFeatures, scope, userId, replay, byokEnabled]);

  const dismiss = useCallback(() => {
    void updateProgress(scope, current => ({ ...current, dismissed: true }));
    setVisible(false);
    onClose?.();
  }, [scope, onClose]);

  const endPractice = async () => {
    await updateProgress(scope, () => ({ step: "ended", dismissed: true }));
    setVisible(false);
    if (onClose) onClose();
    else router.push("/workspace/contexts");
  };

  const handleCreateContext = async () => {
    if (!currentWorkspaceId || writing.current) return;
    writing.current = true;
    setBusy(true);
    setWritePending(true);
    setActionError(null);
    const claimed = { step: "creating" } as const;
    try {
      if (!await claimPractice(scope, "context", claimed)) { setStep("recovery"); return; }
      const ctx = await createContext({ name: practiceContextName(), display_name: "My First Context", is_private: true, expected_workspace_id: currentWorkspaceId });
      if (ctx.workspace_id !== currentWorkspaceId || !ctx.is_private || ctx.is_public || ctx.created_by !== userId) {
        setStep("recovery"); return;
      }
      if (!await advancePractice(scope, claimed, { step: "memory", contextId: ctx.id })) { setStep("recovery"); return; }
      onCreated?.(ctx);
      setContextId(ctx.id);
      setStep("memory");
    } catch (error) {
      if (error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 409) await advancePractice(scope, claimed, { step: "context" });
      else setStep("recovery");
      setActionError(t("errors.createFailed"));
    } finally {
      writing.current = false;
      setWritePending(false);
      setBusy(false);
    }
  };

  const handleSaveMemory = async () => {
    if (!contextId || !currentWorkspaceId || writing.current) return;
    writing.current = true;
    setBusy(true);
    setWritePending(true);
    setActionError(null);
    const claimed = { step: "saving", contextId } as const;
    try {
      if (!await claimPractice(scope, "memory", claimed)) { setStep("recovery"); return; }
      await rememberMemory({
        private_workspace_id: currentWorkspaceId,
        summary: summary.trim(),
        content: content.trim(),
        type: "note",
        importance: 0.7,
        context: { context_id: contextId },
      });
      if (!await advancePractice(scope, claimed, { step: "recall", contextId })) { setStep("recovery"); return; }
      setStep("recall");
    } catch (error) {
      if (error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 409) await advancePractice(scope, claimed, { step: "memory", contextId });
      else setStep("recovery");
      setActionError(t("errors.saveFailed"));
    } finally {
      writing.current = false;
      setWritePending(false);
      setBusy(false);
    }
  };

  const handleRecall = async () => {
    if (!contextId) return;
    setBusy(true);
    setActionError(null);
    try {
      const res = await recallMemories({
        query: query.trim(),
        k: 3,
        filters: { context_id: contextId },
      });
      setResults(res.results);
      setStep("done");
    } catch {
      setActionError(t("errors.recallFailed"));
    } finally {
      setBusy(false);
    }
  };

  const finish = () => {
    // Honor #246 URL-driven current context: land the user in the context they
    // just built. Deferred to here so mid-flow navigation can't remount/hide
    // the card.
    dismiss();
    if (contextId) {
      router.push(`/workspace/contexts?context=${contextId}`);
    }
  };

  if (!ready || !visible) return null;

  const stepIndex = step === "context" ? 1 : step === "memory" ? 2 : 3;

  return (
    <Dialog open={visible} onOpenChange={open => { if (!open && !writePending) dismiss(); }}>
      {/* Keep the panel opaque while it slides in so text stays legible over the backdrop. */}
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-xl data-[state=open]:!fade-in-100"
        onEscapeKeyDown={event => { if (writePending) event.preventDefault(); }}
        onInteractOutside={event => { if (writePending) event.preventDefault(); }}>
        <DialogHeader className="pr-6">
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-brand-green-600 dark:text-brand-green-400" />
            {t("title")}
          </DialogTitle>
          <DialogDescription className="text-slate-600 dark:text-slate-300">{t("subtitle")}</DialogDescription>
        </DialogHeader>
        <div>
          {actionError && <Alert variant="destructive" className="mb-4"><AlertDescription>{actionError}</AlertDescription></Alert>}
        {step === "recovery" ? (
          <Alert><AlertDescription>
            <p>{t("recovery")}</p>
            <Button variant="link" onClick={() => { dismiss(); router.push("/workspace/contexts"); }}>{t("openContexts")}</Button>
            <Button variant="ghost" onClick={endPractice}>{t("endPractice")}</Button>
          </AlertDescription></Alert>
        ) : !hasKey ? (
          // Informational gating notice (not an error): remember/recall need a
          // workspace embedding key. Point at the existing key-setup surface.
          <Alert>
            <KeyRound className="h-4 w-4" />
            <AlertTitle>{t("needsKey.title")}</AlertTitle>
            <AlertDescription className="space-y-3">
              <p>{t("needsKey.body")}</p>
              {/* Navigate to the known frontend route, NOT the backend's
                  external_keys_url — that value is "/integrations/external-keys"
                  (missing the /workspace route-group prefix) and 404s. */}
              <Button
                variant="outline"
                onClick={() =>
                  router.push("/workspace/integrations/external-keys")
                }
              >
                <KeyRound className="mr-2 h-4 w-4" />
                {t("needsKey.button")}
              </Button>
            </AlertDescription>
          </Alert>
        ) : (
          <div className="space-y-4">
            {step !== "done" && (
              <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                {t("stepOf", { current: stepIndex, total: 3 })}
              </p>
            )}

            {step === "context" && (
              <div className="space-y-3">
                <div>
                  <h3 className="font-semibold">{t("context.title")}</h3>
                </div>
                <Button
                  onClick={handleCreateContext}
                  disabled={busy}
                >
                  {busy && <InlineSpinner />}
                  {t("context.createButton")}
                  <ArrowRight className="ml-2 h-4 w-4" />
                </Button>
              </div>
            )}

            {step === "memory" && (
              <div className="space-y-3">
                <div>
                  <h3 className="font-semibold">{t("memory.title")}</h3>
                  <p className="text-sm text-muted-foreground">
                    {t("memory.body")}
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="onboarding-summary">
                    {t("memory.summaryLabel")}
                  </Label>
                  <Input
                    id="onboarding-summary"
                    value={summary}
                    onChange={(e) => setSummary(e.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="onboarding-content">
                    {t("memory.contentLabel")}
                  </Label>
                  <Textarea
                    id="onboarding-content"
                    rows={3}
                    value={content}
                    onChange={(e) => setContent(e.target.value)}
                  />
                </div>
                <Button
                  onClick={handleSaveMemory}
                  disabled={
                    busy || summary.trim().length < 10 || !content.trim()
                  }
                >
                  {busy && <InlineSpinner />}
                  {t("memory.saveButton")}
                  <ArrowRight className="ml-2 h-4 w-4" />
                </Button>
              </div>
            )}

            {step === "recall" && (
              <div className="space-y-3">
                <div>
                  <h3 className="font-semibold">{t("recall.title")}</h3>
                  <p className="text-sm text-muted-foreground">
                    {t("recall.body")}
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="onboarding-query">
                    {t("recall.queryLabel")}
                  </Label>
                  <Input
                    id="onboarding-query"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                  />
                </div>
                <Button onClick={handleRecall} disabled={busy || !query.trim()}>
                  {busy && <InlineSpinner />}
                  <Search className="mr-2 h-4 w-4" />
                  {t("recall.searchButton")}
                </Button>
              </div>
            )}

            {step !== "done" && (
              <p className="border-t pt-3 text-sm text-muted-foreground">
                {t("connectNow")}{" "}
                <Link href={MCP_CONNECTION_HREF} className="text-primary underline">
                  {t("mcp.link")}
                </Link>
              </p>
            )}

            {step === "done" && (
              <div className="space-y-4">
                <Alert>
                  <Check className="h-4 w-4" />
                  <AlertTitle>{t("done.title")}</AlertTitle>
                  <AlertDescription>{t("done.body")}</AlertDescription>
                </Alert>

                {results.length > 0 ? (
                  <div className="space-y-2">
                    <p className="text-sm font-medium">
                      {t("recall.resultsHeading")}
                    </p>
                    <ul className="space-y-2">
                      {results.map((r) => (
                        <li
                          key={r.memory_id}
                          className="rounded-md border bg-muted/30 p-3 text-sm"
                        >
                          <span>{r.summary}</span>
                          {typeof r.score === "number" && (
                            <span className="ml-2 text-xs text-muted-foreground">
                              {t("recall.scoreLabel", {
                                score: Math.round(r.score * 100),
                              })}
                            </span>
                          )}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : (
                  // Recall can legitimately return nothing (edited query,
                  // indexing lag right after remember) — don't leave the
                  // "value moment" screen blank.
                  <p className="text-sm text-muted-foreground">
                    {t("recall.noResults")}
                  </p>
                )}

                <div className="rounded-md border p-3">
                  <h4 className="mb-1 text-sm font-semibold">
                    {t("explainer.heading")}
                  </h4>
                  <p className="text-sm">
                    <span className="font-medium">
                      {t("explainer.recallTitle")}
                    </span>{" "}
                    — {t("explainer.recallBody")}
                  </p>
                  <p className="mt-1 text-sm">
                    <span className="font-medium">
                      {t("explainer.exploreTitle")}
                    </span>{" "}
                    — {t("explainer.exploreBody")}
                  </p>
                </div>

                <div className="rounded-md border border-dashed p-3">
                  <h4 className="mb-1 text-sm font-semibold">
                    {t("mcp.heading")}
                  </h4>
                  <p className="text-sm text-muted-foreground">
                    {t("mcp.body")}
                  </p>
                  <Button asChild variant="link" className="h-auto p-0">
                    <Link href={MCP_CONNECTION_HREF}>
                      {t("mcp.link")}
                      <ExternalLink className="ml-1 h-3 w-3" />
                    </Link>
                  </Button>
                </div>

                <Button onClick={finish}>{t("finish")}</Button>
              </div>
            )}
          </div>
        )}
        </div>
        <Button variant="ghost" disabled={writePending} onClick={dismiss} className="justify-self-end">
          {t("dismiss")}
        </Button>
      </DialogContent>
    </Dialog>
  );
}

export default OnboardingCard;
