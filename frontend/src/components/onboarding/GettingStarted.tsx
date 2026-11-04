"use client";

import { useEffect, useId, useRef, useState } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ContextualHelp } from "@/components/common/ContextualHelp";
import { recallMemories } from "@/lib/api/memory";
import type { Context } from "@/lib/types/context";
import type { RecallResultItem } from "@/lib/types/memory";
import { OnboardingCard } from "./OnboardingCard";
import { readProgress } from "./progress";

type AccessibleContext = Pick<Context, "id" | "name" | "display_name">;

function GuideSearch({ contextId }: { contextId: string }) {
  const t = useTranslations("gettingStarted");
  const queryId = useId();
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<RecallResultItem[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const generation = useRef(0);
  return <>
    <Link className="text-primary underline" href={`/workspace/contexts/${encodeURIComponent(contextId)}`}>{t("openContext")}</Link>
    <form className="mt-3 space-y-2" onSubmit={async event => {
      event.preventDefault();
      if (!query.trim() || busy) return;
      const request = ++generation.current;
      setBusy(true); setFailed(false); setHits(null);
      try {
        const data = await recallMemories({ query: query.trim(), k: 3, filters: { context_id: contextId } });
        if (generation.current === request) setHits(data.results);
      } catch {
        if (generation.current === request) setFailed(true);
      } finally {
        if (generation.current === request) setBusy(false);
      }
    }}>
      <label htmlFor={queryId}>{t("queryLabel")}</label>
      <Input id={queryId} value={query} placeholder={t("example")} onChange={event => {
        ++generation.current; setQuery(event.target.value); setHits(null); setFailed(false); setBusy(false);
      }} />
      <Button disabled={busy || !query.trim()} type="submit">{t("find")}</Button>
      <div aria-live="polite">
        {failed && <p>{t("searchFailed")}</p>}
        {hits?.length === 0 && <p>{t("noHits")}</p>}
        {hits && hits.length > 0 && <ul className="list-disc pl-5">{hits.map(hit => <li key={hit.memory_id}>{hit.summary}</li>)}</ul>}
      </div>
    </form>
  </>;
}

export function GettingStarted({ scope, contexts, owner, admin, onCreateShared, onCreated }: {
  scope: string; contexts: AccessibleContext[]; owner: boolean; admin?: boolean;
  onCreateShared?: () => void; onCreated?: (context: Context) => void;
}) {
  const t = useTranslations("gettingStarted");
  const integration = useTranslations("integrationEntry");
  const panelId = useId();
  const selectId = useId();
  const button = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(() => {
    try { return localStorage.getItem(scope + ":guide") !== "closed"; } catch { return true; }
  });
  const [selected, setSelected] = useState("");
  const [practice, setPractice] = useState(false);
  const launcher = useRef<HTMLButtonElement>(null);
  const returnFocus = useRef(false);
  useEffect(() => {
    if (!practice && returnFocus.current) { (launcher.current ?? button.current)?.focus(); returnFocus.current = false; }
  }, [practice]);
  const current = contexts.find(ctx => ctx.id === selected) ?? contexts[0];
  const savedStep = readProgress(scope).step;
  const canPractice = owner && savedStep !== "ended" && (contexts.length === 0 || savedStep !== "context");
  const toggle = (next: boolean) => {
    setOpen(next);
    try { localStorage.setItem(scope + ":guide", next ? "open" : "closed"); } catch { /* Session-only fallback. */ }
    if (!next) { setPractice(false); button.current?.focus(); }
  };
  return <section className="mb-5 rounded-lg border p-4">
    <Button ref={button} type="button" variant="outline" aria-expanded={open} aria-controls={panelId} onClick={() => toggle(!open)}>{t("title")}</Button>
    {open && <div id={panelId} className="mt-3 space-y-3">
      <p>{t("intro")}</p>
      <Link href="/workspace/integrations/credentials?tab=api-keys#mcp-connection" className="text-primary underline">{integration("tasks.client.title")}</Link>
      {current ? <div className="space-y-2">
        <label htmlFor={selectId}>{t("choose")}</label>
        <select id={selectId} className="block max-w-full rounded border p-2" value={current.id} onChange={e => setSelected(e.target.value)}>
          {contexts.map(ctx => <option key={ctx.id} value={ctx.id}>{ctx.display_name || ctx.name}</option>)}
        </select>
        <GuideSearch key={current.id} contextId={current.id} />
      </div> : admin ? <div><p>{t("adminEmpty")}</p>{onCreateShared && <Button variant="link" onClick={onCreateShared}>{t("createShared")}</Button>}</div> : <p>{t(owner ? "ownerEmpty" : "noContexts")}</p>}
      <ContextualHelp topic="search" />
      {canPractice && <div>{practice ? <OnboardingCard replay onCreated={onCreated} onClose={() => { returnFocus.current = true; setPractice(false); }} /> :
        <Button ref={launcher} type="button" variant="outline" onClick={() => setPractice(true)}>{t("practice")}</Button>}</div>}
      <Button type="button" variant="ghost" onClick={() => toggle(false)}>{t("skip")}</Button>
    </div>}
  </section>;
}
