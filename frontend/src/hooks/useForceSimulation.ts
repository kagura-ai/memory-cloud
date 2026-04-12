"use client";

/**
 * useForceSimulation — d3-force lifecycle hook for Graph tab.
 *
 * Builds a d3-force simulation and drives it via requestAnimationFrame.
 * SVG nodes/edges are rendered as raw DOM (no React reconciliation per tick).
 * Supports zoom/pan (d3-zoom) and node dragging (d3-drag).
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
import { select } from "d3-selection";
import { zoom, zoomIdentity, type ZoomBehavior } from "d3-zoom";
import { drag, type D3DragEvent } from "d3-drag";
import type { GraphNode, GraphEdge } from "@/lib/types/graph";
import type { PresetConfig } from "@/lib/graph/forcePresets";

const SVG_NS = "http://www.w3.org/2000/svg";
const STABLE_ALPHA = 0.001;
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

    // Root group for zoom/pan transform
    const rootGroup = document.createElementNS(SVG_NS, "g");
    rootGroup.setAttribute("data-role", "zoom-root");
    svg.appendChild(rootGroup);

    const edgeGroup = document.createElementNS(SVG_NS, "g");
    edgeGroup.setAttribute("data-role", "edges");
    edgeGroup.setAttribute("stroke", "currentColor");
    edgeGroup.setAttribute("stroke-opacity", "0.4");
    rootGroup.appendChild(edgeGroup);

    const nodeGroup = document.createElementNS(SVG_NS, "g");
    nodeGroup.setAttribute("data-role", "nodes");
    rootGroup.appendChild(nodeGroup);

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
    const circleToNode = new Map<SVGCircleElement, SimNode>();
    for (const node of simNodes) {
      const circle = document.createElementNS(SVG_NS, "circle");
      circle.setAttribute("r", "6");
      circle.setAttribute("fill", colorRef.current(node));
      circle.setAttribute("stroke", "var(--background)");
      circle.setAttribute("stroke-width", "1.5");
      circle.setAttribute("tabindex", "0");
      circle.setAttribute("role", "img");
      circle.setAttribute("aria-label", node.summary || node.id);
      circle.setAttribute("cursor", "grab");
      nodeGroup.appendChild(circle);
      circles.push(circle);
      circleToNode.set(circle, node);
    }

    // Hover listeners
    const handleEnter = (ev: Event) => {
      const n = circleToNode.get(ev.currentTarget as SVGCircleElement);
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

    // Build simulation — keep running (alphaMin very low) so drag can reheat
    const simulation = forceSimulation<SimNode>(simNodes)
      .alphaDecay(curPreset.alphaDecay)
      .alphaMin(STABLE_ALPHA)
      .velocityDecay(0.4)
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

    // Reduced-motion: batch ticks synchronously, draw once, freeze (no drag/zoom)
    const prefersReducedMotion =
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;

    if (prefersReducedMotion) {
      simulation.stop();
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

    // --- Zoom & Pan (d3-zoom) ---
    const svgSelection = select(svg);
    const rootSelection = select(rootGroup);

    const zoomBehavior: ZoomBehavior<SVGSVGElement, unknown> = zoom<
      SVGSVGElement,
      unknown
    >()
      .scaleExtent([0.2, 5])
      .on("zoom", (event) => {
        rootSelection.attr("transform", event.transform.toString());
      });

    svgSelection.call(zoomBehavior);

    // Double-click resets zoom (immediate, no d3-transition dep)
    svgSelection.on("dblclick.zoom", () => {
      svgSelection.call(zoomBehavior.transform, zoomIdentity);
    });

    // --- Node Drag (d3-drag) ---
    type DragEvent = D3DragEvent<SVGCircleElement, SimNode, SimNode>;

    const dragBehavior = drag<SVGCircleElement, SimNode>()
      .on("start", (event: DragEvent) => {
        if (!event.active) simulation.alphaTarget(0.3).restart();
        const d = event.subject;
        d.fx = d.x;
        d.fy = d.y;
        (event.sourceEvent.currentTarget as SVGCircleElement)?.setAttribute(
          "cursor",
          "grabbing",
        );
      })
      .on("drag", (event: DragEvent) => {
        const d = event.subject;
        d.fx = event.x;
        d.fy = event.y;
      })
      .on("end", (event: DragEvent) => {
        if (!event.active) simulation.alphaTarget(0);
        const d = event.subject;
        d.fx = null;
        d.fy = null;
        (event.sourceEvent.currentTarget as SVGCircleElement)?.setAttribute(
          "cursor",
          "grab",
        );
      });

    // Attach drag to each circle, binding the SimNode as datum
    for (let i = 0; i < circles.length; i++) {
      const sel = select<SVGCircleElement, SimNode>(circles[i]).datum(
        simNodes[i],
      );
      sel.call(dragBehavior);
    }

    // Use simulation's own tick event + RAF for rendering
    simulation.on("tick", writeDom);

    // Initial positions
    writeDom();

    return () => {
      simulation.stop();
      simulation.on("tick", null);
      svgSelection.on(".zoom", null);
      detachListeners();
    };
  }, [nodes, edges, preset, width, height, svgRef]);
}
