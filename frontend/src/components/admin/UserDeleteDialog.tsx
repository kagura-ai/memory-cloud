"use client";

/**
 * Admin user deletion (#2420).
 *
 * A confirm step, then, when the API answers 409 "Ownership transfer
 * required", a successor picker built from that payload: a member for each
 * workspace the user owns, a successor for each shared context it created
 * (by default the workspace's successor, where the workspace is handed over)
 * and, read-only, the connector write targets, which go to the workspace's
 * owner. A workspace nobody else is in has no successor to pick: the admin
 * may tick it to delete it with the user instead, its contexts included
 * (#2640). The DELETE is then sent again with the selections. Another 409 with
 * the picker (the user's resources changed meanwhile) refreshes it; any other
 * refusal, at either step, is shown in the dialog (the picks are kept).
 */

import { useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { AlertCircle, Info } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useToast } from "@/hooks/use-toast";
import {
  deleteUser,
  userDeleteFailure,
  type TransferMember,
  type TransferPicker,
  type TransferWorkspace,
} from "@/lib/api/admin";

import { USER_DELETE_DIALOG_TEST_IDS as IDS } from "./user-delete-dialog.testids";

export interface UserDeleteTarget {
  id: string;
  name: string;
}

interface UserDeleteDialogProps {
  user: UserDeleteTarget;
  /** Closed without deleting (cancel). */
  onClose: () => void;
  /** The user was deleted (the success toast is shown). */
  onDeleted: () => void;
}

/** A context's choice "whoever takes its workspace": sends no selection. */
const SAME_AS_WORKSPACE = "__workspace__";

const WORKSPACE_ROLES: readonly string[] = ["owner", "admin", "member", "viewer"];

type Picks = Record<string, string>;

function byId<T extends { id: string }>(...lists: T[][]): T[] {
  const merged = new Map<string, T>();
  for (const list of lists) for (const item of list) merged.set(item.id, item);
  return [...merged.values()];
}

/**
 * A later 409 merged into the picker shown: its entries replace those of the
 * same id, the rows already shown stay (their picks satisfied the server), and
 * its connector write targets are authoritative.
 */
function mergePicker(
  shown: TransferPicker | null,
  next: TransferPicker,
): TransferPicker {
  if (shown === null) return next;
  const fixed = new Set(next.ownerFixedContexts.map((c) => c.id));
  return {
    workspaceIds: [...new Set([...shown.workspaceIds, ...next.workspaceIds])],
    workspaces: byId(shown.workspaces, next.workspaces),
    contexts: byId(shown.contexts, next.contexts).filter(
      (c) => !fixed.has(c.id),
    ),
    ownerFixedContexts: next.ownerFixedContexts,
  };
}

function keep(picks: Picks, valid: (id: string, value: string) => boolean): Picks {
  return Object.fromEntries(
    Object.entries(picks).filter(([id, value]) => valid(id, value)),
  );
}

export function UserDeleteDialog({
  user,
  onClose,
  onDeleted,
}: UserDeleteDialogProps) {
  const t = useTranslations("admin.users");
  const tCommon = useTranslations("admin.common");
  const { toast } = useToast();

  const [picker, setPicker] = useState<TransferPicker | null>(null);
  const [changed, setChanged] = useState(false);
  const [busy, setBusy] = useState(false);
  // A refusal of the DELETE, shown in the dialog (#2420 review).
  const [error, setError] = useState<string | null>(null);
  const [workspacePicks, setWorkspacePicks] = useState<Picks>({});
  const [contextPicks, setContextPicks] = useState<Picks>({});
  // Workspaces nobody else is in, ticked to be deleted with the user (#2640).
  const [deleteWorkspaces, setDeleteWorkspaces] = useState<Set<string>>(
    () => new Set(),
  );

  const workspaces = useMemo(
    () => new Map((picker?.workspaces ?? []).map((w) => [w.id, w])),
    [picker],
  );
  const handedOver = useMemo(
    () => new Set(picker?.workspaceIds ?? []),
    [picker],
  );
  const workspaceRows: TransferWorkspace[] = (picker?.workspaceIds ?? []).map(
    (id) => workspaces.get(id) ?? { id, name: id, owner_user_id: "", members: [] },
  );
  // Only an explicit tick on a workspace with no other member deletes it.
  const deleting = new Set(
    workspaceRows
      .filter((w) => w.members.length === 0 && deleteWorkspaces.has(w.id))
      .map((w) => w.id),
  );
  // What is in a workspace deleted with the user goes with it: no successor.
  const contextRows = (picker?.contexts ?? []).filter(
    (c) => !deleting.has(c.workspace_id),
  );
  const ownerFixedRows = (picker?.ownerFixedContexts ?? []).filter(
    (c) => !deleting.has(c.workspace_id),
  );

  const contextChoice = (contextId: string, workspaceId: string) =>
    contextPicks[contextId] ??
    (handedOver.has(workspaceId) ? SAME_AS_WORKSPACE : undefined);

  const complete =
    workspaceRows.every((w) => workspacePicks[w.id] || deleting.has(w.id)) &&
    contextRows.every((c) => contextChoice(c.id, c.workspace_id) !== undefined);

  const roleLabel = (role: string) =>
    WORKSPACE_ROLES.includes(role) ? t(`detail.roles.${role}`) : role;

  const memberLabel = (m: TransferMember) => {
    const who =
      m.name && m.email ? `${m.name} (${m.email})` : m.name || m.email || m.user_id;
    return `${who} · ${roleLabel(m.role)}`;
  };

  const nameOf = (userId: string) => {
    for (const w of workspaces.values()) {
      const m = w.members.find((member) => member.user_id === userId);
      if (m) return m.name || m.email || userId;
    }
    return userId;
  };

  const workspaceName = (id: string) => workspaces.get(id)?.name ?? id;

  const showPicker = (next: TransferPicker) => {
    const merged = mergePicker(picker, next);
    const members = new Map(
      merged.workspaces.map((w) => [w.id, new Set(w.members.map((m) => m.user_id))]),
    );
    const candidates = new Map(
      merged.contexts.map((c) => [c.id, new Set(c.candidate_user_ids)]),
    );
    setChanged(picker !== null);
    setError(null);
    setPicker(merged);
    // A pick the refreshed list no longer offers is cleared.
    setWorkspacePicks((prev) =>
      keep(prev, (id, value) => members.get(id)?.has(value) ?? false),
    );
    setContextPicks((prev) =>
      keep(
        prev,
        (id, value) =>
          candidates.has(id) &&
          (value === SAME_AS_WORKSPACE || (candidates.get(id)?.has(value) ?? false)),
      ),
    );
    // A workspace someone joined meanwhile takes a successor again.
    setDeleteWorkspaces(
      (prev) => new Set([...prev].filter((id) => members.get(id)?.size === 0)),
    );
  };

  const setDeleting = (workspaceId: string, on: boolean) =>
    setDeleteWorkspaces((prev) => {
      const next = new Set(prev);
      if (on) next.add(workspaceId);
      else next.delete(workspaceId);
      return next;
    });

  const send = async () => {
    setBusy(true);
    setError(null);
    try {
      if (picker === null) {
        await deleteUser(user.id);
      } else {
        await deleteUser(user.id, {
          workspace_transfers: Object.fromEntries(
            workspaceRows.flatMap((w) =>
              !deleting.has(w.id) && workspacePicks[w.id]
                ? [[w.id, workspacePicks[w.id]]]
                : [],
            ),
          ),
          // Only explicit picks: "same as the workspace successor" is the
          // server's default for a context of a workspace handed over.
          context_transfers: Object.fromEntries(
            contextRows.flatMap((c) => {
              const value = contextPicks[c.id];
              return value && value !== SAME_AS_WORKSPACE ? [[c.id, value]] : [];
            }),
          ),
          ...(deleting.size > 0 ? { delete_workspaces: [...deleting] } : {}),
        });
      }
      toast({
        title: tCommon("success"),
        description: t("messages.deleteSuccess", { name: user.name }),
      });
      onDeleted();
    } catch (error) {
      const failure = userDeleteFailure(error);
      if (failure.kind === "transfer_required" && failure.picker) {
        showPicker(failure.picker);
        return;
      }
      const description =
        failure.kind === "transfer_required"
          ? t("messages.deleteTransferRequired", {
              workspaces: failure.workspaces,
              contexts: failure.contexts,
            })
          : failure.kind === "message"
            ? t("messages.deleteErrorWithReason", { reason: failure.message })
            : t("messages.deleteError");
      // Shown in the dialog, not as a toast behind it (frontend error-surface
      // rule). The picks stay: a successor that lost access can be changed,
      // or the dialog cancelled and opened again for a fresh list.
      setError(description);
    } finally {
      setBusy(false);
    }
  };

  const errorAlert = error && (
    <Alert variant="destructive" data-testid={IDS.error}>
      <AlertCircle className="h-4 w-4" />
      <AlertDescription>{error}</AlertDescription>
    </Alert>
  );

  const confirmStep = (
    <>
      <DialogHeader>
        <DialogTitle>{t("deleteDialog.title", { name: user.name })}</DialogTitle>
        <DialogDescription>{t("deleteDialog.handover")}</DialogDescription>
      </DialogHeader>
      <Alert variant="destructive">
        <AlertCircle className="h-4 w-4" />
        <AlertDescription>{t("deleteDialog.deleted")}</AlertDescription>
      </Alert>
      {errorAlert}
      <DialogFooter>
        <Button variant="outline" onClick={onClose} disabled={busy}>
          {tCommon("cancel")}
        </Button>
        <Button
          variant="destructive"
          onClick={send}
          disabled={busy}
          data-testid={IDS.confirm}
        >
          {busy ? t("deleteDialog.deleting") : t("deleteDialog.confirm")}
        </Button>
      </DialogFooter>
    </>
  );

  const pickStep = (
    <>
      <DialogHeader>
        <DialogTitle>
          {t("deleteDialog.pickTitle", { name: user.name })}
        </DialogTitle>
        <DialogDescription>{t("deleteDialog.pickDescription")}</DialogDescription>
      </DialogHeader>
      {changed && (
        <Alert data-testid={IDS.changed}>
          <Info className="h-4 w-4" />
          <AlertDescription>{t("deleteDialog.changed")}</AlertDescription>
        </Alert>
      )}
      {errorAlert}

      {workspaceRows.length > 0 && (
        <section className="space-y-3">
          <h3 className="text-sm font-semibold">{t("deleteDialog.workspaces")}</h3>
          {workspaceRows.map((w) => (
            <div
              key={w.id}
              className="space-y-1.5"
              data-testid={IDS.workspaceRow(w.id)}
            >
              <p className="text-sm font-medium">{w.name}</p>
              {w.members.length === 0 ? (
                <div className="space-y-1.5">
                  <p className="text-xs text-muted-foreground">
                    {t("deleteDialog.noMembers")}
                  </p>
                  <label
                    htmlFor={IDS.deleteWorkspace(w.id)}
                    className="flex cursor-pointer items-start gap-2 text-sm"
                  >
                    <Checkbox
                      id={IDS.deleteWorkspace(w.id)}
                      data-testid={IDS.deleteWorkspace(w.id)}
                      className="mt-0.5"
                      checked={deleting.has(w.id)}
                      onCheckedChange={(checked) => setDeleting(w.id, checked === true)}
                      disabled={busy}
                    />
                    {t("deleteDialog.deleteWithUser")}
                  </label>
                </div>
              ) : (
                <Select
                  value={workspacePicks[w.id] ?? ""}
                  onValueChange={(value) =>
                    setWorkspacePicks((prev) => ({ ...prev, [w.id]: value }))
                  }
                  disabled={busy}
                >
                  <SelectTrigger
                    aria-label={t("deleteDialog.workspaceSuccessor", {
                      workspace: w.name,
                    })}
                  >
                    <SelectValue placeholder={t("deleteDialog.placeholder")} />
                  </SelectTrigger>
                  <SelectContent>
                    {w.members.map((m) => (
                      <SelectItem key={m.user_id} value={m.user_id}>
                        {memberLabel(m)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </div>
          ))}
        </section>
      )}

      {contextRows.length > 0 && (
        <section className="space-y-3">
          <h3 className="text-sm font-semibold">{t("deleteDialog.contexts")}</h3>
          {contextRows.map((c) => {
            const members = workspaces.get(c.workspace_id)?.members ?? [];
            const offered = c.candidate_user_ids.map(
              (id) =>
                members.find((m) => m.user_id === id) ?? {
                  user_id: id,
                  name: null,
                  email: null,
                  role: "",
                },
            );
            const sameOffered = handedOver.has(c.workspace_id);
            const label = c.display_name || c.name;
            return (
              <div
                key={c.id}
                className="space-y-1.5"
                data-testid={IDS.contextRow(c.id)}
              >
                <p className="text-sm font-medium">
                  {label}{" "}
                  <span className="font-normal text-muted-foreground">
                    {t("deleteDialog.inWorkspace", {
                      workspace: workspaceName(c.workspace_id),
                    })}
                  </span>
                </p>
                {!sameOffered && offered.length === 0 ? (
                  <p className="text-xs text-muted-foreground">
                    {t("deleteDialog.noCandidates")}
                  </p>
                ) : (
                  <Select
                    value={contextChoice(c.id, c.workspace_id) ?? ""}
                    onValueChange={(value) =>
                      setContextPicks((prev) => ({ ...prev, [c.id]: value }))
                    }
                    disabled={busy}
                  >
                    <SelectTrigger
                      aria-label={t("deleteDialog.contextSuccessor", {
                        context: label,
                      })}
                    >
                      <SelectValue placeholder={t("deleteDialog.placeholder")} />
                    </SelectTrigger>
                    <SelectContent>
                      {sameOffered && (
                        <SelectItem value={SAME_AS_WORKSPACE}>
                          {t("deleteDialog.sameAsWorkspace")}
                        </SelectItem>
                      )}
                      {offered.map((m) => (
                        <SelectItem key={m.user_id} value={m.user_id}>
                          {memberLabel(m)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              </div>
            );
          })}
        </section>
      )}

      {ownerFixedRows.length > 0 && (
        <section className="space-y-3">
          <div>
            <h3 className="text-sm font-semibold">{t("deleteDialog.ownerFixed")}</h3>
            <p className="text-xs text-muted-foreground">
              {t("deleteDialog.ownerFixedHint")}
            </p>
          </div>
          {ownerFixedRows.map((c) => {
            const successor = workspacePicks[c.workspace_id];
            return (
              <div
                key={c.id}
                className="space-y-0.5"
                data-testid={IDS.ownerFixedRow(c.id)}
              >
                <p className="text-sm font-medium">
                  {c.display_name || c.name}{" "}
                  <span className="font-normal text-muted-foreground">
                    {t("deleteDialog.inWorkspace", {
                      workspace: workspaceName(c.workspace_id),
                    })}
                  </span>
                </p>
                <p className="text-sm text-muted-foreground">
                  {c.successor_user_id
                    ? t("deleteDialog.ownerFixedOwner", {
                        name: nameOf(c.successor_user_id),
                      })
                    : successor
                      ? t("deleteDialog.ownerFixedFollowsPicked", {
                          name: nameOf(successor),
                        })
                      : t("deleteDialog.ownerFixedFollows")}
                </p>
              </div>
            );
          })}
        </section>
      )}

      <DialogFooter>
        <Button variant="outline" onClick={onClose} disabled={busy}>
          {tCommon("cancel")}
        </Button>
        <Button
          variant="destructive"
          onClick={send}
          disabled={busy || !complete}
          data-testid={IDS.submit}
        >
          {busy ? t("deleteDialog.deleting") : t("deleteDialog.submit")}
        </Button>
      </DialogFooter>
    </>
  );

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-[560px]">
        {picker === null ? confirmStep : pickStep}
      </DialogContent>
    </Dialog>
  );
}
