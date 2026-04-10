"use client";

import { useTranslations, useLocale } from "next-intl";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Calendar } from "lucide-react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { formatDate } from "@/lib/utils/datetime";
import { useAuth } from "@/contexts/AuthContext";
import type { MemoryTimelineResponse } from "@/lib/api/workspaces";

interface MemoryTimelineChartProps {
  timeline: MemoryTimelineResponse;
  onDaysChange: (days: 7 | 30) => void;
  days: 7 | 30;
}

export function MemoryTimelineChart({
  timeline,
  onDaysChange,
  days,
}: MemoryTimelineChartProps) {
  const t = useTranslations("workspace");
  const { user: authUser } = useAuth();
  const locale = useLocale();

  return (
    <div className="mb-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-2xl font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2">
          <Calendar className="h-6 w-6" />
          {t("memoryTimeline")}
        </h2>
        <div className="flex gap-2">
          <Button
            variant={days === 7 ? "default" : "outline"}
            size="sm"
            onClick={() => onDaysChange(7)}
          >
            7 {t("days")}
          </Button>
          <Button
            variant={days === 30 ? "default" : "outline"}
            size="sm"
            onClick={() => onDaysChange(30)}
          >
            30 {t("days")}
          </Button>
        </div>
      </div>

      <Card>
        <CardContent className="pt-6">
          <ResponsiveContainer width="100%" height={300}>
            <LineChart data={timeline.daily_counts}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis
                dataKey="date"
                tickFormatter={(date) =>
                  formatDate(date, authUser?.timezone, locale)
                }
              />
              <YAxis />
              <Tooltip
                labelFormatter={(date) =>
                  formatDate(date, authUser?.timezone, locale)
                }
              />
              <Line
                type="monotone"
                dataKey="count"
                stroke="#3b82f6"
                strokeWidth={2}
                name={t("memoriesCreated")}
                dot={{ fill: "#3b82f6", r: 3 }}
              />
            </LineChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>
    </div>
  );
}
