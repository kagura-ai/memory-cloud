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
 *  - Trigger: workspace role >= Admin AND zero contexts AND not dismissed
 *    (localStorage). No server-side "seen" flag — re-showing on a fresh device
 *    or after deleting all contexts is acceptable for 1.0.
 *  - Hybrid value moment: a minimal in-app remember()/recall() makes the first
 *    recall return a recognizable hit, WITHOUT pulling in the #951 importer.
 *  - recall-vs-explore explainer is COPY ONLY: recall does NOT auto-trigger
 *    explore (mixing graph signals into recall degrades precision, #120/#216).
 *  - Non-modal dismissible card on the dashboard — OUT of the auth critical
 *    path, so an onboarding bug can't block users from their data.
 *  - The `?context=<id>` navigation (URL-driven current context, #246) is
 *    deferred to the final "Finish" CTA: navigating mid-flow would remount this
 *    card and (with 1 context now) hide it, killing the stepper.
 *  - remember/recall need a workspace embedding key; if absent we show a
 *    non-error "set up embeddings" notice instead of a guaranteed-to-fail flow.
 */

import { useState, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import {
  Sparkles,
  X,
  ArrowRight,
  Check,
  Search,
  KeyRound,
  ExternalLink,
} from "lucide-react";

import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useToast } from "@/hooks/use-toast";
import { hasWorkspaceRole, WorkspaceRole } from "@/lib/auth/rbac";
import { getContexts, createContext } from "@/lib/api/contexts";
import { rememberMemory, recallMemories } from "@/lib/api/memory";
import { checkOpenAIKeyStatus } from "@/lib/api/workspaces";
import type { RecallResultItem } from "@/lib/types/memory";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { InlineSpinner } from "@/components/common/LoadingState";

const DISMISS_KEY = "onboarding:dismissed";
type Step = "context" | "memory" | "recall" | "done";

export function OnboardingCard() {
  const t = useTranslations("onboarding");
  const router = useRouter();
  const { toast } = useToast();
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
  const [contextName, setContextName] = useState(() => t("context.sampleName"));
  const [summary, setSummary] = useState(() => t("memory.sampleSummary"));
  const [content, setContent] = useState(() => t("memory.sampleContent"));
  const [query, setQuery] = useState(() => t("recall.sampleQuery"));
  const [results, setResults] = useState<RecallResultItem[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    // Wait for the workspace to hydrate — current_user_role is null mid-load.
    if (workspaceLoading || !currentWorkspace || !currentWorkspaceId) return;

    const dismissed =
      typeof window !== "undefined" &&
      window.localStorage.getItem(DISMISS_KEY) === "true";
    const canCreate = hasWorkspaceRole(
      currentWorkspace.current_user_role,
      WorkspaceRole.Admin,
    );
    if (dismissed || !canCreate) {
      setReady(true);
      return;
    }

    (async () => {
      try {
        const ctx = await getContexts();
        if (!alive) return;
        if (ctx.contexts.length === 0) {
          // Only probe the embedding key when the card will actually show —
          // users with contexts (the vast majority of loads) never see it.
          const keyStatus = await checkOpenAIKeyStatus(currentWorkspaceId).catch(
            () => null,
          );
          if (!alive) return;
          if (keyStatus) setHasKey(keyStatus.has_key);
          setVisible(true);
        }
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
  }, [workspaceLoading, currentWorkspace, currentWorkspaceId]);

  const dismiss = useCallback(() => {
    if (typeof window !== "undefined") {
      window.localStorage.setItem(DISMISS_KEY, "true");
    }
    setVisible(false);
  }, []);

  const handleCreateContext = async () => {
    const name = contextName.trim();
    if (!name) return;
    setBusy(true);
    try {
      const ctx = await createContext({ name, is_private: true });
      setContextId(ctx.id);
      setStep("memory");
    } catch {
      toast({ variant: "destructive", title: t("errors.createFailed") });
    } finally {
      setBusy(false);
    }
  };

  const handleSaveMemory = async () => {
    if (!contextId) return;
    setBusy(true);
    try {
      await rememberMemory({
        summary: summary.trim(),
        content: content.trim(),
        type: "note",
        importance: 0.7,
        context: { context_id: contextId },
      });
      setStep("recall");
    } catch {
      toast({ variant: "destructive", title: t("errors.saveFailed") });
    } finally {
      setBusy(false);
    }
  };

  const handleRecall = async () => {
    if (!contextId) return;
    setBusy(true);
    try {
      const res = await recallMemories({
        query: query.trim(),
        k: 3,
        filters: { context_id: contextId },
      });
      setResults(res.results);
      setStep("done");
    } catch {
      toast({ variant: "destructive", title: t("errors.recallFailed") });
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
    <Card className="mb-6 border-brand-green-300 dark:border-brand-green-800/60">
      <CardHeader>
        <div className="flex items-start justify-between gap-4">
          <div>
            <CardTitle className="flex items-center gap-2">
              <Sparkles className="h-5 w-5 text-brand-green-600 dark:text-brand-green-400" />
              {t("title")}
            </CardTitle>
            <CardDescription className="mt-1">{t("subtitle")}</CardDescription>
          </div>
          <Button
            variant="ghost"
            size="icon"
            onClick={dismiss}
            aria-label={t("dismiss")}
          >
            <X className="h-4 w-4" />
          </Button>
        </div>
      </CardHeader>

      <CardContent>
        {!hasKey ? (
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
                  <p className="text-sm text-muted-foreground">
                    {t("context.body")}
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="onboarding-context-name">
                    {t("context.nameLabel")}
                  </Label>
                  <Input
                    id="onboarding-context-name"
                    value={contextName}
                    onChange={(e) => setContextName(e.target.value)}
                  />
                </div>
                <Button
                  onClick={handleCreateContext}
                  disabled={busy || !contextName.trim()}
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
                  disabled={busy || summary.trim().length < 10 || !content.trim()}
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
                  <Button
                    variant="link"
                    className="h-auto p-0"
                    onClick={() =>
                      router.push(
                        "/workspace/integrations/credentials?tab=api-keys",
                      )
                    }
                  >
                    {t("mcp.link")}
                    <ExternalLink className="ml-1 h-3 w-3" />
                  </Button>
                </div>

                <Button onClick={finish}>{t("finish")}</Button>
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default OnboardingCard;
