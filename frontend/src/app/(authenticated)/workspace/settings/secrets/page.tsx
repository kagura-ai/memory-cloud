"use client";

/**
 * Workspace Settings > Secrets (#1134, server #1128)
 *
 * Owner/admin management surface for the zero-knowledge secret store. This is
 * the **revoke-only / management** console:
 *   - list secrets (name, status, version, grant count, rotation badge),
 *   - list recipient pubkeys + owner approve/revoke (TOFU trust gate),
 *   - revoke a recipient's grant on a secret,
 *   - verify the tamper-evident audit chain.
 *
 * HARD INVARIANT: this page NEVER renders a plaintext secret value or the age
 * ciphertext, and never offers a "reveal" affordance. Encrypt/decrypt + put/get
 * live entirely in the `kagura secret` CLI/SDK (the server is zero-knowledge and
 * the browser must not hold a private key or plaintext). The API client this
 * page imports deliberately does not even wrap the put/fetch endpoints.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations, useLocale } from "next-intl";
import { KeyRound, ShieldCheck, ShieldAlert, RotateCw } from "lucide-react";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { Section } from "@/components/common/Section";
import { TableLoadingState } from "@/components/common/LoadingState";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { EmptyState } from "@/components/ui/empty-state";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useAuth } from "@/contexts/AuthContext";
import { useToast } from "@/hooks/use-toast";
import { formatDateTime } from "@/lib/utils/datetime";
import {
  listSecrets,
  listSecretPubkeys,
  approveSecretPubkey,
  revokeSecretPubkey,
  revokeSecretGrant,
  verifySecretAudit,
  type SecretMeta,
  type SecretPubkey,
  type SecretPubkeyStatus,
  type AuditVerifyResult,
} from "@/lib/api/secrets";

type PubkeyAction = "approve" | "revoke";

export default function WorkspaceSecretsPage() {
  const t = useTranslations("workspace");
  const tCommon = useTranslations("common");
  const locale = useLocale();
  const { currentWorkspaceId, currentWorkspace } = useWorkspace();
  const { user } = useAuth();
  const { toast } = useToast();

  const role = currentWorkspace?.current_user_role;
  const isOwner = role === "owner";
  const isAdmin = role === "owner" || role === "admin";

  const [secrets, setSecrets] = useState<SecretMeta[]>([]);
  const [pubkeys, setPubkeys] = useState<SecretPubkey[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Pubkey approve/revoke confirmation (owner only).
  const [pubkeyDialog, setPubkeyDialog] = useState<{
    action: PubkeyAction;
    pubkey: SecretPubkey;
  } | null>(null);
  const [pubkeyBusy, setPubkeyBusy] = useState(false);

  // Grant revocation: a secret is fixed by the row; the operator picks which
  // active recipient pubkey to revoke (there is no list-grants endpoint).
  const [grantDialog, setGrantDialog] = useState<SecretMeta | null>(null);
  const [grantPubkeyId, setGrantPubkeyId] = useState("");
  const [grantBusy, setGrantBusy] = useState(false);

  // Audit chain verification (owner/admin).
  const [auditResult, setAuditResult] = useState<AuditVerifyResult | null>(
    null,
  );
  const [auditBusy, setAuditBusy] = useState(false);

  const load = useCallback(async () => {
    if (!currentWorkspaceId || !isAdmin) return;
    try {
      setLoading(true);
      setError(null);
      const [s, p] = await Promise.all([listSecrets(), listSecretPubkeys()]);
      setSecrets(s);
      setPubkeys(p);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [currentWorkspaceId, isAdmin]);

  useEffect(() => {
    load();
  }, [load]);

  const activePubkeys = pubkeys.filter((p) => p.status === "active");

  const confirmPubkeyAction = async () => {
    if (!pubkeyDialog) return;
    const { action, pubkey } = pubkeyDialog;
    try {
      setPubkeyBusy(true);
      if (action === "approve") {
        await approveSecretPubkey(pubkey.id);
      } else {
        await revokeSecretPubkey(pubkey.id);
      }
      setPubkeyDialog(null);
      await load();
      toast({
        title: tCommon("success"),
        description:
          action === "approve"
            ? t("secretStore.approveSuccess")
            : t("secretStore.revokeKeySuccess"),
      });
    } catch (e) {
      toast({
        variant: "destructive",
        title: tCommon("error"),
        description: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setPubkeyBusy(false);
    }
  };

  const confirmRevokeGrant = async () => {
    if (!grantDialog || !grantPubkeyId) return;
    try {
      setGrantBusy(true);
      await revokeSecretGrant(grantDialog.name, grantPubkeyId);
      setGrantDialog(null);
      setGrantPubkeyId("");
      await load();
      toast({
        title: tCommon("success"),
        description: t("secretStore.revokeGrantSuccess"),
      });
    } catch (e) {
      toast({
        variant: "destructive",
        title: tCommon("error"),
        description: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setGrantBusy(false);
    }
  };

  const runAudit = async () => {
    try {
      setAuditBusy(true);
      setAuditResult(await verifySecretAudit());
    } catch (e) {
      toast({
        variant: "destructive",
        title: tCommon("error"),
        description: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setAuditBusy(false);
    }
  };

  const pubkeyStatusBadge = (status: SecretPubkeyStatus) => {
    const cls =
      status === "active"
        ? "text-green-700 dark:text-green-400 bg-green-100 dark:bg-green-900/20"
        : status === "pending"
          ? "text-amber-800 dark:text-amber-200 bg-amber-100 dark:bg-amber-900/20"
          : "text-gray-600 dark:text-gray-400 bg-gray-100 dark:bg-gray-800";
    return (
      <span className={`rounded px-2 py-1 text-xs ${cls}`}>
        {t(`secretStore.status_${status}`)}
      </span>
    );
  };

  if (!isAdmin) {
    return (
      <PageContainer>
        <PageHeader
          title={t("secretStore.title")}
          description={t("secretStore.description")}
        />
        <Alert>
          <AlertDescription>{t("secretStore.notAuthorized")}</AlertDescription>
        </Alert>
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader
        title={t("secretStore.title")}
        description={t("secretStore.description")}
      />

      {/* Hard invariant, surfaced to the operator: this console manages access
          only — it never shows secret values. Informational notice (not an
          error channel) per .claude/rules/frontend.md. */}
      <Alert>
        <ShieldCheck className="h-4 w-4" />
        <AlertDescription>
          {t("secretStore.zeroKnowledgeNote")}
        </AlertDescription>
      </Alert>

      <ErrorBanner error={error} />

      {/* ---- Secrets ---- */}
      <Section
        title={t("secretStore.secretsTitle")}
        description={t("secretStore.secretsDesc")}
      >
        {loading ? (
          <TableLoadingState rows={3} />
        ) : error ? null : secrets.length === 0 ? (
          <EmptyState
            icon={KeyRound}
            title={t("secretStore.noSecretsTitle")}
            description={t("secretStore.noSecretsDesc")}
          />
        ) : (
          <div className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("secretStore.colName")}</TableHead>
                  <TableHead>{t("secretStore.colStatus")}</TableHead>
                  <TableHead>{t("secretStore.colVersion")}</TableHead>
                  <TableHead>{t("secretStore.colGrants")}</TableHead>
                  <TableHead className="text-right">
                    {t("secretStore.colActions")}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {secrets.map((s) => (
                  <TableRow key={s.name}>
                    <TableCell>
                      <code className="rounded bg-gray-100 px-1.5 py-0.5 text-xs dark:bg-gray-900">
                        {s.name}
                      </code>
                    </TableCell>
                    <TableCell>
                      {s.rotation_needed ? (
                        <span className="inline-flex items-center gap-1 rounded bg-red-100 px-2 py-1 text-xs text-red-700 dark:bg-red-900/20 dark:text-red-400">
                          <ShieldAlert className="h-3 w-3" />
                          {t("secretStore.rotationNeeded")}
                        </span>
                      ) : (
                        <span className="rounded bg-gray-100 px-2 py-1 text-xs text-gray-600 dark:bg-gray-800 dark:text-gray-400">
                          {s.status}
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="text-sm">
                      {s.current_version ?? "—"}
                    </TableCell>
                    <TableCell className="text-sm">{s.grant_count}</TableCell>
                    <TableCell className="text-right">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        disabled={
                          s.grant_count === 0 || activePubkeys.length === 0
                        }
                        onClick={() => {
                          setGrantPubkeyId("");
                          setGrantDialog(s);
                        }}
                      >
                        {t("secretStore.revokeGrant")}
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </Section>

      {/* ---- Recipient pubkeys ---- */}
      <Section
        title={t("secretStore.pubkeysTitle")}
        description={t("secretStore.pubkeysDesc")}
      >
        {!isOwner && (
          <Alert className="mb-3">
            <AlertDescription>
              {t("secretStore.ownerOnlyKeyActions")}
            </AlertDescription>
          </Alert>
        )}
        {loading ? (
          <TableLoadingState rows={3} />
        ) : error ? null : pubkeys.length === 0 ? (
          <EmptyState
            icon={ShieldCheck}
            title={t("secretStore.noPubkeysTitle")}
            description={t("secretStore.noPubkeysDesc")}
          />
        ) : (
          <div className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("secretStore.colFingerprint")}</TableHead>
                  <TableHead>{t("secretStore.colLabel")}</TableHead>
                  <TableHead>{t("secretStore.colIdentity")}</TableHead>
                  <TableHead>{t("secretStore.colStatus")}</TableHead>
                  <TableHead className="text-right">
                    {t("secretStore.colActions")}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {pubkeys.map((p) => (
                  <TableRow key={p.id}>
                    <TableCell>
                      {/* Fingerprint shown in full so the owner can verify it
                          out-of-band (TOFU) before approving. */}
                      <code
                        className="break-all text-xs"
                        title={t("secretStore.fingerprintTitle")}
                      >
                        {p.fingerprint}
                      </code>
                    </TableCell>
                    <TableCell className="text-sm">{p.label ?? "—"}</TableCell>
                    <TableCell className="text-xs text-gray-500 dark:text-gray-400">
                      {p.identity_id}
                    </TableCell>
                    <TableCell>{pubkeyStatusBadge(p.status)}</TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-2">
                        {isOwner && p.status === "pending" && (
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            onClick={() =>
                              setPubkeyDialog({ action: "approve", pubkey: p })
                            }
                          >
                            {t("secretStore.approve")}
                          </Button>
                        )}
                        {isOwner && p.status !== "revoked" && (
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            className="text-red-600 hover:text-red-700"
                            onClick={() =>
                              setPubkeyDialog({ action: "revoke", pubkey: p })
                            }
                          >
                            {t("secretStore.revokeKey")}
                          </Button>
                        )}
                        {p.attested_at && (
                          <span
                            className="self-center text-xs text-gray-400"
                            title={formatDateTime(
                              p.attested_at,
                              user?.timezone,
                              locale,
                            )}
                          >
                            {t("secretStore.approvedLabel")}
                          </span>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </Section>

      {/* ---- Audit chain ---- */}
      <Section
        title={t("secretStore.auditTitle")}
        description={t("secretStore.auditDesc")}
      >
        <div className="flex flex-wrap items-center gap-3">
          <Button type="button" onClick={runAudit} disabled={auditBusy}>
            <RotateCw
              className={`mr-2 h-4 w-4 ${auditBusy ? "animate-spin" : ""}`}
            />
            {auditBusy ? t("secretStore.verifying") : t("secretStore.verify")}
          </Button>
          {auditResult &&
            (auditResult.valid ? (
              <span className="inline-flex items-center gap-1 text-sm text-green-700 dark:text-green-400">
                <ShieldCheck className="h-4 w-4" />
                {t("secretStore.auditValid", {
                  entries: auditResult.entries ?? 0,
                })}
                {auditResult.head && (
                  <code className="ml-1 rounded bg-gray-100 px-1.5 py-0.5 text-xs dark:bg-gray-900">
                    {auditResult.head.slice(0, 12)}…
                  </code>
                )}
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 text-sm text-red-700 dark:text-red-400">
                <ShieldAlert className="h-4 w-4" />
                {t("secretStore.auditBroken", {
                  id: auditResult.broken_at ?? 0,
                  reason: auditResult.reason ?? "",
                })}
              </span>
            ))}
        </div>
      </Section>

      {/* ---- Pubkey approve/revoke confirmation ---- */}
      <AlertDialog
        open={pubkeyDialog !== null}
        onOpenChange={(open) => !open && setPubkeyDialog(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {pubkeyDialog?.action === "approve"
                ? t("secretStore.approveTitle")
                : t("secretStore.revokeKeyTitle")}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {pubkeyDialog?.action === "approve"
                ? t("secretStore.approveDesc")
                : t("secretStore.revokeKeyDesc")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {pubkeyDialog && (
            <code className="block break-all rounded bg-gray-100 px-2 py-1 text-xs dark:bg-gray-900">
              {pubkeyDialog.pubkey.fingerprint}
            </code>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={pubkeyBusy}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmPubkeyAction}
              disabled={pubkeyBusy}
              className={
                pubkeyDialog?.action === "revoke"
                  ? "bg-red-600 hover:bg-red-700"
                  : undefined
              }
            >
              {pubkeyBusy
                ? tCommon("saving")
                : pubkeyDialog?.action === "approve"
                  ? t("secretStore.approve")
                  : t("secretStore.revokeKey")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* ---- Revoke grant ---- */}
      <AlertDialog
        open={grantDialog !== null}
        onOpenChange={(open) => {
          if (!open) {
            setGrantDialog(null);
            setGrantPubkeyId("");
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t("secretStore.revokeGrantTitle")}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t("secretStore.revokeGrantDesc")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="space-y-2">
            <code className="block rounded bg-gray-100 px-2 py-1 text-xs dark:bg-gray-900">
              {grantDialog?.name}
            </code>
            <label
              htmlFor="revoke-grant-pubkey"
              className="block text-sm font-medium"
            >
              {t("secretStore.revokeGrantSelect")}
            </label>
            <select
              id="revoke-grant-pubkey"
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              value={grantPubkeyId}
              onChange={(e) => setGrantPubkeyId(e.target.value)}
            >
              <option value="">
                {t("secretStore.revokeGrantSelectPlaceholder")}
              </option>
              {activePubkeys.map((p) => (
                <option key={p.id} value={p.id}>
                  {(p.label ? `${p.label} — ` : "") + p.fingerprint}
                </option>
              ))}
            </select>
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={grantBusy}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmRevokeGrant}
              disabled={grantBusy || !grantPubkeyId}
              className="bg-red-600 hover:bg-red-700"
            >
              {grantBusy ? tCommon("saving") : t("secretStore.revokeGrant")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PageContainer>
  );
}
