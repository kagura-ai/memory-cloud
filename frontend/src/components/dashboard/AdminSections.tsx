"use client";

import { useEffect, useState } from "react";
import { useTranslations, useLocale } from "next-intl";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { AlertCircle, Users, ChevronDown } from "lucide-react";
import { InlineSpinner } from "@/components/common/LoadingState";
import { formatRelativeTime } from "@/lib/utils/datetime";
import { useAuth } from "@/contexts/AuthContext";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import {
  getContextUserActivity,
  ContextUserActivityResponse,
  getWorkspaceMemberUsage,
  MemberUsageEntry,
} from "@/lib/api/workspaces";

interface AdminSectionsProps {
  selectedContextId: string | null;
  currentWorkspaceId: string | null;
}

function MemberUsageSection() {
  const t = useTranslations("dashboard");
  const [members, setMembers] = useState<MemberUsageEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();

    getWorkspaceMemberUsage()
      .then((data) => {
        if (!controller.signal.aborted) setMembers(data.members);
      })
      .catch(() => {})
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });

    return () => controller.abort();
  }, []);

  if (loading) return null;
  if (members.length <= 1) return null;

  return (
    <Card>
      <CardContent className="pt-6">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t("member")}</TableHead>
              <TableHead className="text-right">{t("memories")}</TableHead>
              <TableHead className="text-right">{t("apiToday")}</TableHead>
              <TableHead className="text-right">{t("apiWeek")}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {members.map((m) => (
              <TableRow key={m.user_id}>
                <TableCell>
                  <div>
                    <div className="font-medium">{m.name || "Unknown"}</div>
                    {m.email && (
                      <div className="text-xs text-muted-foreground">
                        {m.email}
                      </div>
                    )}
                  </div>
                </TableCell>
                <TableCell className="text-right">
                  {m.memory_count.toLocaleString()}
                </TableCell>
                <TableCell className="text-right">
                  {m.api_calls_today.toLocaleString()}
                </TableCell>
                <TableCell className="text-right">
                  {m.api_calls_week.toLocaleString()}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

function UserActivitySection({
  selectedContextId,
  currentWorkspaceId,
}: {
  selectedContextId: string;
  currentWorkspaceId: string;
}) {
  const t = useTranslations("workspace");
  const tCommon = useTranslations("common");
  const { user: authUser } = useAuth();
  const locale = useLocale();

  const [activityDays, setActivityDays] = useState<7 | 30>(7);
  const [userActivity, setUserActivity] =
    useState<ContextUserActivityResponse | null>(null);
  const [activityError, setActivityError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setActivityError(null);

    getContextUserActivity(currentWorkspaceId, selectedContextId, activityDays)
      .then((activity) => {
        if (!controller.signal.aborted) {
          setUserActivity(activity);
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) {
          setActivityError(err?.message || "Failed to load user activity");
          setUserActivity(null);
        }
      });

    return () => controller.abort();
  }, [selectedContextId, activityDays, currentWorkspaceId]);

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div>
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
            <Users className="h-5 w-5" />
            {t("userActivity")}
          </h3>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            {t("perUserApiCallBreakdown")}
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            variant={activityDays === 7 ? "default" : "outline"}
            size="sm"
            onClick={() => setActivityDays(7)}
          >
            7 {t("days")}
          </Button>
          <Button
            variant={activityDays === 30 ? "default" : "outline"}
            size="sm"
            onClick={() => setActivityDays(30)}
          >
            30 {t("days")}
          </Button>
        </div>
      </div>

      <Card>
        <CardContent className="pt-6">
          {activityError ? (
            <Alert variant="destructive" className="mb-4">
              <AlertCircle className="h-4 w-4" />
              <AlertTitle>{tCommon("error")}</AlertTitle>
              <AlertDescription>{activityError}</AlertDescription>
            </Alert>
          ) : userActivity ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("userName")}</TableHead>
                  <TableHead>{t("email")}</TableHead>
                  <TableHead className="text-right">{t("apiCalls")}</TableHead>
                  <TableHead className="text-right">
                    {t("lastActivity")}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {userActivity.users.length === 0 ? (
                  <TableRow>
                    <TableCell
                      colSpan={4}
                      className="text-center text-muted-foreground py-8"
                    >
                      {t("noUserActivity")}
                    </TableCell>
                  </TableRow>
                ) : (
                  userActivity.users.map((user) => (
                    <TableRow key={user.user_id}>
                      <TableCell className="font-medium">
                        {user.user_name || t("notAvailable")}
                      </TableCell>
                      <TableCell className="text-sm text-gray-600 dark:text-gray-400">
                        {user.user_email || t("notAvailable")}
                      </TableCell>
                      <TableCell className="text-right">
                        <span
                          className={
                            user.api_calls > 0
                              ? "text-green-600 font-medium"
                              : "text-gray-400"
                          }
                        >
                          {user.api_calls.toLocaleString()}
                        </span>
                      </TableCell>
                      <TableCell className="text-right text-sm text-gray-500">
                        {user.last_activity
                          ? formatRelativeTime(
                              user.last_activity,
                              authUser?.timezone,
                              locale,
                            )
                          : "Never"}
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          ) : (
            <div className="flex items-center justify-center py-8">
              <InlineSpinner size="md" />
              <span className="ml-3 text-slate-500">
                {t("loadingUserActivity")}
              </span>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

export function AdminSections({
  selectedContextId,
  currentWorkspaceId,
}: AdminSectionsProps) {
  const t = useTranslations("dashboard");
  const { currentWorkspace } = useWorkspace();

  const isAdmin =
    currentWorkspace?.current_user_role === "admin" ||
    currentWorkspace?.current_user_role === "owner";

  // Hide entirely for non-admin, or when there's nothing to show
  // (solo workspace with no context selected = both subsections would be empty)
  const hasUserActivity = !!(selectedContextId && currentWorkspaceId);
  if (!isAdmin) return null;

  return (
    <Collapsible className="mb-6">
      <CollapsibleTrigger className="group flex items-center gap-2 w-full text-left py-3 px-1 hover:bg-gray-50 dark:hover:bg-gray-800/50 rounded-md transition-colors">
        <ChevronDown className="h-5 w-5 text-gray-500 transition-transform -rotate-90 group-data-[state=open]:rotate-0" />
        <Users className="h-5 w-5 text-gray-500" />
        <span className="text-lg font-semibold text-gray-900 dark:text-gray-100">
          {t("adminMemberActivity")}
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-4 space-y-6">
        {hasUserActivity && (
          <UserActivitySection
            selectedContextId={selectedContextId!}
            currentWorkspaceId={currentWorkspaceId!}
          />
        )}
        <MemberUsageSection />
      </CollapsibleContent>
    </Collapsible>
  );
}
