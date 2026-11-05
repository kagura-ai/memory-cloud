"use client";

import { useEffect, useState } from "react";

interface PageSectionNavigationProps {
  label: string;
  sections: { id: string; label: string }[];
}

export function PageSectionNavigation({ label, sections }: PageSectionNavigationProps) {
  const [activeId, setActiveId] = useState("");
  useEffect(() => {
    const syncHash = () => setActiveId(window.location.hash.slice(1));
    syncHash();
    window.addEventListener("hashchange", syncHash);
    return () => window.removeEventListener("hashchange", syncHash);
  }, []);
  const selectedId = sections.find(section => section.id === activeId)?.id ?? sections[0]?.id;

  return (
    <nav
      aria-label={label}
      className="sticky top-0 z-20 flex gap-1 overflow-x-auto rounded-lg border border-slate-200 bg-slate-100 p-1.5 shadow-sm dark:border-slate-700 dark:bg-slate-800"
    >
      {sections.map(section => (
        <a
          key={section.id}
          href={`#${section.id}`}
          onClick={() => setActiveId(section.id)}
          aria-current={selectedId === section.id ? "location" : undefined}
          className={`inline-flex min-h-11 shrink-0 items-center whitespace-nowrap rounded-md px-3 py-2 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 ${selectedId === section.id
            ? "bg-white text-slate-900 shadow-sm dark:bg-slate-700 dark:text-white"
            : "text-slate-700 hover:bg-white hover:text-slate-900 dark:text-slate-200 dark:hover:bg-slate-700 dark:hover:text-white"}`}
        >
          {section.label}
        </a>
      ))}
    </nav>
  );
}
