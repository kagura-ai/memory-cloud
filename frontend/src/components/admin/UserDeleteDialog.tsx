"use client";

/**
 * Admin user deletion (#2420).
 *
 * A confirm step, then, when the API answers 409 "Ownership transfer
 * required", a successor picker built from that payload: a member for each
 * workspace the user owns, a successor for each shared context it created
 * (by default the workspace's successor, where the workspace is handed over)
 * and, read-only, the connector write targets, which go to the workspace's
 * owner. The DELETE is then sent again with the selections. Another 409 (the
 * user's resources changed meanwhile) refreshes the picker; any other failure
 * closes the dialog with a toast.
 */

import { useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { AlertCircle, Info } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
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
  /** Closed without deleting (cancel, or a refusal already toasted). */
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
    contextIds: [...new Set([...shown.contextIds, ...next.contextIds])].filter(
      (id) => !fixed.has(id),
    ),
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
  const [workspacePicks, setWorkspacePicks] = useState<Picks>({});
  const [contextPicks, setContextPicks] = useState<Picks>({});

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
  const contextRows = picker?.contexts ?? [];

  const contextChoice = (contextId: string, workspaceId: string) =>
    contextPicks[contextId] ??
    (handedOver.has(workspaceId) ? SAME_AS_WORKSPACE : undefined);

  const complete =
    workspaceRows.every((w) => workspacePicks[w.id]) &&
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
  };

  const send = async () => {
    setBusy(true);
    try {
      if (picker === null) {
        await deleteUser(user.id);
      } else {
        await deleteUser(user.id, {
          workspace_transfers: Object.fromEntries(
            workspaceRows.map((w) => [w.id, workspacePicks[w.id]]),
          ),
          // Only explicit picks: "same as the workspace successor" is the
          // server's default for a context of a workspace handed over.
          context_transfers: Object.fromEntries(
            contextRows.flatMap((c) => {
              const value = contextPicks[c.id];
              return value && value !== SAME_AS_WORKSPACE ? [[c.id, value]] : [];
            }),
          ),
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
      onClose();
      toast({
        title: tCommon("error"),
        description:
          failure.kind === "transfer_required"
            ? t("messages.deleteTransferRequired", {
                workspaces: failure.workspaces,
                contexts: failure.contexts,
              })
            : failure.kind === "message"
              ? t("messages.deleteErrorWithReason", { reason: failure.message })
              : t("messages.deleteError"),
        variant: "destructive",
      });
    } finally {
      setBusy(false);
    }
  };

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
                <p className="text-xs text-muted-foreground">
                  {t("deleteDialog.noMembers")}
                </p>
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

      {picker && picker.ownerFixedContexts.length > 0 && (
        <section className="space-y-3">
          <div>
            <h3 className="text-sm font-semibold">{t("deleteDialog.ownerFixed")}</h3>
            <p className="text-xs text-muted-foreground">
              {t("deleteDialog.ownerFixedHint")}
            </p>
          </div>
          {picker.ownerFixedContexts.map((c) => {
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
