"use client";

import { useEffect, useId, useState } from "react";
import { useTranslations } from "next-intl";
import { Check, Plus, X } from "lucide-react";
import { MCP_TOOL_PRESETS } from "@/lib/mcp/toolPresets";
import { apiClient } from "@/lib/api/base";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { InlineSpinner } from "@/components/common/LoadingState";

interface ToolChoice { name: string; description: string }

export function McpToolPicker({ value, onChange, disabled = false }: {
  value: string[];
  onChange: (tools: string[]) => void;
  disabled?: boolean;
}) {
  const t = useTranslations("mcpConnection.toolPicker");
  const descriptions = useTranslations("mcpToolDescriptions");
  const id = useId();
  const [tools, setTools] = useState<ToolChoice[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [query, setQuery] = useState("");

  useEffect(() => {
    let active = true;
    setFailed(false);
    apiClient.get<{ tools: ToolChoice[] }>("/api/v1/mcp/tools")
      .then(result => { if (active) setTools(result.tools); })
      .catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [attempt]);

  const titleFor = (name: string) => descriptions.has(`${name}.title`) ? descriptions(`${name}.title`) : name;
  const search = query.trim().toLowerCase();
  const choices = tools?.map(tool => ({
    ...tool,
    title: titleFor(tool.name),
    summary: descriptions.has(`${tool.name}.description`)
      ? descriptions(`${tool.name}.description`)
      : tool.description.split("\n")[0],
  })).filter(tool =>
    [tool.name, tool.title, tool.summary, tool.description].some(text => text.toLowerCase().includes(search)),
  );

  return (
    <div className="space-y-3">
      <p className="text-sm font-medium">{t("presets")}</p>
      <div className="flex flex-wrap gap-2">
        {MCP_TOOL_PRESETS.map(preset => {
          const available: string[] = preset.tools.filter(name => tools?.some(tool => tool.name === name));
          const selected = available.length > 0 && available.every(name => value.includes(name));
          return (
            <Button
              key={preset.key}
              type="button"
              variant={selected ? "secondary" : "outline"}
              size="sm"
              aria-pressed={selected}
              disabled={disabled || available.length === 0}
              className={selected ? "border border-blue-400 bg-blue-100 text-blue-900 hover:bg-blue-200 dark:border-blue-600 dark:bg-blue-950 dark:text-blue-100" : ""}
              onClick={() => onChange(selected
                ? value.filter(name => !available.includes(name))
                : [...new Set([...value, ...available])])}
            >
              {selected
                ? <Check aria-hidden="true" className="mr-1 h-3.5 w-3.5" />
                : <Plus aria-hidden="true" className="mr-1 h-3.5 w-3.5" />}
              {t(`preset.${preset.key}`)}
            </Button>
          );
        })}
      </div>
      <p className="text-xs text-muted-foreground">{t("presetHint")}</p>
      {value.length > 0 && (
        <ul aria-label={t("selected")} className="flex flex-wrap gap-2">
          {value.map(name => (
            <li key={name} className="inline-flex items-center rounded-full border border-blue-300 bg-blue-100 pl-3 text-sm text-blue-900 dark:border-blue-700 dark:bg-blue-950 dark:text-blue-100">
              <span title={name}>{titleFor(name)}</span>
              <Button type="button" variant="ghost" size="icon" className="ml-1 h-8 w-8 rounded-full" disabled={disabled} aria-label={t("remove", { name: titleFor(name) })} onClick={() => onChange(value.filter(item => item !== name))}>
                <X aria-hidden="true" className="h-3.5 w-3.5" />
              </Button>
            </li>
          ))}
        </ul>
      )}
      <Input type="search" value={query} onChange={event => setQuery(event.target.value)} disabled={disabled} aria-label={t("search")} placeholder={t("search")} className="bg-white dark:bg-slate-900" />
      {failed ? (
        <div>
          <ErrorBanner error={t("loadError")} />
          <Button type="button" variant="outline" size="sm" onClick={() => setAttempt(previous => previous + 1)}>{t("retry")}</Button>
        </div>
      ) : choices ? (
        <div className="max-h-56 space-y-1 overflow-y-auto rounded-md border border-slate-200 bg-white p-2 dark:border-slate-600 dark:bg-slate-900">
          {choices.length === 0 && <p className="p-2 text-sm text-muted-foreground">{t("noMatches")}</p>}
          {choices.map(tool => (
            <div key={tool.name} className="flex items-start gap-2 rounded px-2 py-2 hover:bg-slate-100 dark:hover:bg-slate-800">
              <Checkbox id={id + tool.name} disabled={disabled} checked={value.includes(tool.name)} onCheckedChange={checked => onChange(checked ? [...value, tool.name] : value.filter(name => name !== tool.name))} />
              <Label htmlFor={id + tool.name} className="min-w-0 flex-1 cursor-pointer space-y-1">
                <span className="block text-sm font-medium">{tool.title}</span>
                <code className="block text-xs font-normal text-muted-foreground">{tool.name}</code>
                <span className="block line-clamp-1 text-xs font-normal text-muted-foreground">{tool.summary}</span>
              </Label>
            </div>
          ))}
        </div>
      ) : <InlineSpinner />}
    </div>
  );
}
