/**
 * ProtectionSection
 *
 * Self-contained section for context lock/unlock and delete.
 * Rendered at the bottom of the Settings tab, after SearchSettingsSection.
 * Extracted from SettingsTabPanel (#232).
 */

"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
import { Trash2, Loader2, ShieldCheck, ShieldOff } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { updateContext, deleteContext } from "@/lib/api/contexts";
import type { Context } from "@/lib/types/context";
import { useAuth } from "@/contexts/AuthContext";

interface ProtectionSectionProps {
  context: Context;
}

export function ProtectionSection({ context }: ProtectionSectionProps) {
  const router = useRouter();
  const { refetchUser } = useAuth();
  const { toast } = useToast();
  const t = useTranslations("contextSettings");
  const tCommon = useTranslations("common");

  const [isLocked, setIsLocked] = useState(context.is_locked ?? false);
  const [lockSaving, setLockSaving] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const handleLockToggle = async () => {
    const newLocked = !isLocked;
    try {
      setLockSaving(true);
      await updateContext(context.id, { is_locked: newLocked });
      setIsLocked(newLocked);
      toast({
        title: newLocked ? t("lockedToast") : t("unlockedToast"),
        description: newLocked ? t("lockedToastDesc") : t("unlockedToastDesc"),
      });
    } catch {
      toast({
        title: t("lockFailed"),
        variant: "destructive",
      });
    } finally {
      setLockSaving(false);
    }
  };

  const handleDelete = async () => {
    try {
      setDeleting(true);
      await deleteContext(context.id);
      await refetchUser();
      router.push("/workspace/contexts");
    } catch (err: unknown) {
      const apiError = err as { details?: { detail?: string } };
      toast({
        title: apiError?.details?.detail || t("deleteFailed"),
        variant: "destructive",
      });
      setDeleteDialogOpen(false);
    } finally {
      setDeleting(false);
    }
  };

  if (context.is_default) return null;

  return (
    <>
      <Card className="border-red-200 dark:border-red-900">
        <CardHeader>
          <CardTitle className="text-red-900 dark:text-red-400">
            {t("protectionTitle")}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between p-4 border rounded-lg">
            <div className="flex-1">
              <p className="font-medium text-sm flex items-center gap-2">
                {isLocked ? (
                  <ShieldCheck className="h-4 w-4 text-amber-600" />
                ) : (
                  <ShieldOff className="h-4 w-4 text-muted-foreground" />
                )}
                {isLocked ? t("contextLocked") : t("contextUnlocked")}
              </p>
              <p className="text-sm text-muted-foreground">
                {isLocked ? t("lockedDesc") : t("unlockedDesc")}
              </p>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={handleLockToggle}
              disabled={lockSaving}
            >
              {lockSaving ? t("saving") : isLocked ? t("unlock") : t("lock")}
            </Button>
          </div>

          <div
            className={`p-4 border border-red-200 dark:border-red-900 rounded-lg ${isLocked ? "opacity-50" : ""}`}
          >
            <p className="font-semibold text-red-900 dark:text-red-400 mb-2">
              {t("deleteContext")}
            </p>
            <p className="text-sm text-muted-foreground mb-4">
              {isLocked
                ? t("deleteLockedDesc", { name: context.name })
                : t("deleteDesc", { name: context.name })}
            </p>
            <Button
              variant="destructive"
              size="sm"
              onClick={() => setDeleteDialogOpen(true)}
              disabled={isLocked}
            >
              <Trash2 className="h-4 w-4 mr-2" />
              {t("deleteContext")}
            </Button>
          </div>
        </CardContent>
      </Card>

      <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("deleteConfirmTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("deleteConfirmDesc", { name: context.name })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{tCommon("cancel")}</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDelete}
              className="bg-red-600 hover:bg-red-700"
              disabled={deleting}
            >
              {deleting && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              {t("deleteContext")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
