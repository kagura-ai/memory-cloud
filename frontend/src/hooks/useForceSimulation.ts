/**
 * useForceSimulation — d3-force lifecycle hook for Graph tab.
 *
 * Builds a d3-force simulation and drives it via requestAnimationFrame.
 * SVG nodes/edges are rendered as raw DOM (no React reconciliation per tick).
 * Auto-restarts when nodes, edges, preset, or dimensions change.
 * Respects prefers-reduced-motion by running a synchronous tick batch.
 *
 * Issue #233 — bounded neural graph visualization.
 */

import { useEffect, useRef } from "react";
import type { RefObject } from "react";
import {
  forceSimulation,
  forceLink,
  forceManyBody,
  forceCenter,
  forceCollide,
  forceRadial,
  type SimulationNodeDatum,
  type SimulationLinkDatum,
} from "d3-force";
import type { GraphNode, GraphEdge } from "@/lib/types/graph";
import type { PresetConfig } from "@/lib/graph/forcePresets";

const SVG_NS = "http://www.w3.org/2000/svg";
const STABLE_ALPHA = 0.01;
const STATIC_TICK_BUDGET = 300;

type SimNode = GraphNode & SimulationNodeDatum;
type SimLink = SimulationLinkDatum<SimNode> & { weight: number };

interface UseForceSimulationInput {
  nodes: readonly GraphNode[];
  edges: readonly GraphEdge[];
  preset: PresetConfig;
  width: number;
  height: number;
  svgRef: RefObject<SVGSVGElement | null>;
  onHoverChange: (node: GraphNode | null) => void;
  colorForNode: (node: GraphNode) => string;
}

export function useForceSimulation({
  nodes,
  edges,
  preset,
  width,
  height,
  svgRef,
  onHoverChange,
  colorForNode,
}: UseForceSimulationInput): void {
  const nodesRef = useRef(nodes);
  const edgesRef = useRef(edges);
  const presetRef = useRef(preset);
  const widthRef = useRef(width);
  const heightRef = useRef(height);
  const onHoverRef = useRef(onHoverChange);
  const colorRef = useRef(colorForNode);

  nodesRef.current = nodes;
  edgesRef.current = edges;
  presetRef.current = preset;
  widthRef.current = width;
  heightRef.current = height;
  onHoverRef.current = onHoverChange;
  colorRef.current = colorForNode;

  useEffect(() => {
    const svg = svgRef.current;
    const curNodes = nodesRef.current;
    const curEdges = edgesRef.current;
    const curPreset = presetRef.current;
    const w = widthRef.current;
    const h = heightRef.current;

    if (!svg || w <= 0 || h <= 0 || curNodes.length === 0) return;

    // Clear SVG
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    const edgeGroup = document.createElementNS(SVG_NS, "g");
    edgeGroup.setAttribute("data-role", "edges");
    edgeGroup.setAttribute("stroke", "currentColor");
    edgeGroup.setAttribute("stroke-opacity", "0.4");
    svg.appendChild(edgeGroup);

    const nodeGroup = document.createElementNS(SVG_NS, "g");
    nodeGroup.setAttribute("data-role", "nodes");
    svg.appendChild(nodeGroup);

    // Build sim data
    const simNodes: SimNode[] = curNodes.map((n) => ({
      ...n,
      x: (Math.random() * 0.6 + 0.2) * w,
      y: (Math.random() * 0.6 + 0.2) * h,
    }));
    const nodeById = new Map(simNodes.map((n) => [n.id, n]));
    const simLinks: SimLink[] = [];
    for (const e of curEdges) {
      const src = nodeById.get(e.source);
      const tgt = nodeById.get(e.target);
      if (!src || !tgt) continue;
      simLinks.push({ source: src, target: tgt, weight: e.weight });
    }

    // Create DOM elements
    const lines: SVGLineElement[] = [];
    for (let i = 0; i < simLinks.length; i++) {
      const line = document.createElementNS(SVG_NS, "line");
      line.setAttribute("stroke-width", "1");
      edgeGroup.appendChild(line);
      lines.push(line);
    }

    const circles: SVGCircleElement[] = [];
    const hoverMap = new Map<SVGCircleElement, SimNode>();
    for (const node of simNodes) {
      const circle = document.createElementNS(SVG_NS, "circle");
      circle.setAttribute("r", "6");
      circle.setAttribute("fill", colorRef.current(node));
      circle.setAttribute("stroke", "var(--background)");
      circle.setAttribute("stroke-width", "1.5");
      circle.setAttribute("tabindex", "0");
      circle.setAttribute("role", "img");
      circle.setAttribute("aria-label", node.summary || node.id);
      circle.setAttribute("cursor", "pointer");
      nodeGroup.appendChild(circle);
      circles.push(circle);
      hoverMap.set(circle, node);
    }

    // Hover listeners
    const handleEnter = (ev: Event) => {
      const n = hoverMap.get(ev.currentTarget as SVGCircleElement);
      if (n) onHoverRef.current(n);
    };
    const handleLeave = () => onHoverRef.current(null);
    const attachListeners = () => {
      for (const c of circles) {
        c.addEventListener("mouseenter", handleEnter);
        c.addEventListener("mouseleave", handleLeave);
        c.addEventListener("focus", handleEnter);
        c.addEventListener("blur", handleLeave);
      }
    };
    const detachListeners = () => {
      for (const c of circles) {
        c.removeEventListener("mouseenter", handleEnter);
        c.removeEventListener("mouseleave", handleLeave);
        c.removeEventListener("focus", handleEnter);
        c.removeEventListener("blur", handleLeave);
      }
    };
    attachListeners();

    // Build simulation
    const simulation = forceSimulation<SimNode>(simNodes)
      .alphaDecay(curPreset.alphaDecay)
      .force(
        "link",
        forceLink<SimNode, SimLink>(simLinks)
          .strength(curPreset.linkStrength)
          .distance(curPreset.linkDistance),
      )
      .force(
        "charge",
        forceManyBody<SimNode>().strength(curPreset.chargeStrength),
      )
      .force("center", forceCenter<SimNode>(w / 2, h / 2));

    if (curPreset.collisionRadius !== null) {
      simulation.force(
        "collision",
        forceCollide<SimNode>(curPreset.collisionRadius),
      );
    }
    if (curPreset.radialRadius !== null) {
      simulation.force(
        "radial",
        forceRadial<SimNode>(curPreset.radialRadius, w / 2, h / 2),
      );
    }

    simulation.stop();

    // DOM writer
    const writeDom = () => {
      for (let i = 0; i < simNodes.length; i++) {
        const n = simNodes[i];
        circles[i].setAttribute("cx", String(n.x ?? 0));
        circles[i].setAttribute("cy", String(n.y ?? 0));
      }
      for (let i = 0; i < simLinks.length; i++) {
        const s = simLinks[i].source as SimNode;
        const t = simLinks[i].target as SimNode;
        lines[i].setAttribute("x1", String(s.x ?? 0));
        lines[i].setAttribute("y1", String(s.y ?? 0));
        lines[i].setAttribute("x2", String(t.x ?? 0));
        lines[i].setAttribute("y2", String(t.y ?? 0));
      }
    };
    writeDom();

    // Reduced-motion: batch ticks synchronously, draw once, freeze
    const prefersReducedMotion =
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;

    if (prefersReducedMotion) {
      for (
        let i = 0;
        i < STATIC_TICK_BUDGET && simulation.alpha() > STABLE_ALPHA;
        i++
      ) {
        simulation.tick();
      }
      writeDom();
      return () => {
        detachListeners();
        simulation.stop();
      };
    }

    // Normal RAF loop
    let cancelled = false;
    let rafId: number | null = null;
    const step = () => {
      if (cancelled) return;
      simulation.tick();
      writeDom();
      if (simulation.alpha() > STABLE_ALPHA) {
        rafId = requestAnimationFrame(step);
      }
    };
    rafId = requestAnimationFrame(step);

    return () => {
      cancelled = true;
      if (rafId !== null) cancelAnimationFrame(rafId);
      simulation.stop();
      detachListeners();
    };
  }, [nodes, edges, preset, width, height, svgRef]);
}
