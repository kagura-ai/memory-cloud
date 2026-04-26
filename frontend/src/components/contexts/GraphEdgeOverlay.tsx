"use client";

/**
 * GraphEdgeOverlay — floating metadata card for a clicked graph edge.
 *
 * Container-relative absolute <div> so it clips inside the graph canvas's
 * `overflow-hidden` wrapper — same pattern as the existing legend and hover
 * tooltip in GraphTabPanel. Coordinates `x`/`y` are container-relative,
 * computed by the caller from the click event's clientX/clientY minus the
 * container's getBoundingClientRect().
 *
 * Dismisses on Escape and outside-click. Optional fields (`created_at`,
 * `confidence`) only render their row when the value is present.
 */

import { useEffect, useRef } from "react";
import { useLocale, useTranslations } from "next-intl";
import { X } from "lucide-react";
import type { GraphEdge } from "@/lib/types/graph";
import { useAuth } from "@/contexts/AuthContext";
import { formatDateTime } from "@/lib/utils/datetime";

// Approximate dimensions used to clamp the overlay inside the container.
// They do not need to be exact — close-enough avoids the overlay being
// pushed outside `overflow-hidden`. If the actual rendered card is larger
// (long edge type names), the container clips harmlessly.
const APPROX_WIDTH = 260;
const APPROX_HEIGHT = 160;
const EDGE_PAD = 8;

function clamp(value: number, min: number, max: number): number {
  if (max < min) return min;
  return Math.min(Math.max(value, min), max);
}

export interface GraphEdgeOverlayProps {
  edge: GraphEdge;
  // Resolved titles (caller looks them up in the nodes array). Falls back to
  // the edge endpoint id when the node is not present in the filtered set.
  sourceTitle: string;
  targetTitle: string;
  // Container-relative coordinates of the click point.
  x: number;
  y: number;
  containerWidth: number;
  containerHeight: number;
  onClose: () => void;
}

export function GraphEdgeOverlay({
  edge,
  sourceTitle,
  targetTitle,
  x,
  y,
  containerWidth,
  containerHeight,
  onClose,
}: GraphEdgeOverlayProps) {
  const t = useTranslations("contexts");
  const locale = useLocale();
  const { user } = useAuth();
  const cardRef = useRef<HTMLDivElement | null>(null);

  // Esc and outside-click dismissal. Both listeners attach on mount and
  // detach on unmount, so the overlay does not leak handlers across the
  // edge-replace UX (caller swaps `selectedEdge` and the component remounts
  // with new coordinates anyway).
  useEffect(() => {
    const handleKeyDown = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") onClose();
    };
    const handleMouseDown = (ev: MouseEvent) => {
      const node = cardRef.current;
      if (!node) return;
      if (ev.target instanceof Node && node.contains(ev.target)) return;
      onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    // ``mousedown`` (not ``click``) so we dismiss before the underlying
    // SVG sees the next click — otherwise clicking another edge would close
    // this overlay AND open a new one in the same tick, which can race the
    // state update order.
    document.addEventListener("mousedown", handleMouseDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("mousedown", handleMouseDown);
    };
  }, [onClose]);

  const left = clamp(
    x + EDGE_PAD,
    EDGE_PAD,
    Math.max(EDGE_PAD, containerWidth - APPROX_WIDTH - EDGE_PAD),
  );
  const top = clamp(
    y + EDGE_PAD,
    EDGE_PAD,
    Math.max(EDGE_PAD, containerHeight - APPROX_HEIGHT - EDGE_PAD),
  );

  return (
    <div
      ref={cardRef}
      role="dialog"
      aria-label={t("graphEdgeOverlayTitle")}
      className="absolute z-10 max-w-xs text-xs text-slate-700 dark:text-slate-200 bg-white/95 dark:bg-black/85 backdrop-blur px-3 py-2 rounded-md border border-slate-200 dark:border-slate-700 shadow-md space-y-1"
      style={{ left, top }}
    >
      <button
        type="button"
        onClick={onClose}
        aria-label={t("graphEdgeClose")}
        className="absolute top-1 right-1 text-slate-400 hover:text-slate-700 dark:text-slate-500 dark:hover:text-slate-200"
      >
        <X className="h-3.5 w-3.5" />
      </button>
      <div className="pr-5">
        <span className="font-semibold">{t("graphSource")}: </span>
        <span className="break-words">{sourceTitle}</span>
      </div>
      <div>
        <span className="font-semibold">{t("graphTarget")}: </span>
        <span className="break-words">{targetTitle}</span>
      </div>
      <div>
        <span className="font-semibold">{t("graphType")}: </span>
        <span>{edge.type}</span>
      </div>
      <div>
        <span className="font-semibold">{t("graphWeight")}: </span>
        <span>{edge.weight.toFixed(2)}</span>
      </div>
      {edge.confidence !== undefined && (
        <div>
          <span className="font-semibold">{t("graphEdgeConfidence")}: </span>
          <span>{edge.confidence.toFixed(2)}</span>
        </div>
      )}
      {edge.created_at && (
        <div>
          <span className="font-semibold">{t("graphEdgeCreatedAt")}: </span>
          <span>{formatDateTime(edge.created_at, user?.timezone, locale)}</span>
        </div>
      )}
    </div>
  );
}
