"use client";

/**
 * useForceSimulation — d3-force lifecycle hook for Graph tab.
 *
 * Builds a d3-force simulation driven by its internal tick timer.
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
  // Node click is discriminated from drag INSIDE d3-drag's start/end events
  // (a separate onPointerDown/onPointerUp would race d3-drag's pointer
  // capture). Edge click delivers viewport coords so the caller can place a
  // container-relative overlay via getBoundingClientRect().
  onNodeClick?: (node: GraphNode) => void;
  onEdgeClick?: (edge: GraphEdge, clientX: number, clientY: number) => void;
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
  onNodeClick,
  onEdgeClick,
}: UseForceSimulationInput): void {
  const nodesRef = useRef(nodes);
  const edgesRef = useRef(edges);
  const presetRef = useRef(preset);
  const widthRef = useRef(width);
  const heightRef = useRef(height);
  const onHoverRef = useRef(onHoverChange);
  const colorRef = useRef(colorForNode);
  const onNodeClickRef = useRef(onNodeClick);
  const onEdgeClickRef = useRef(onEdgeClick);

  nodesRef.current = nodes;
  edgesRef.current = edges;
  presetRef.current = preset;
  widthRef.current = width;
  heightRef.current = height;
  onHoverRef.current = onHoverChange;
  colorRef.current = colorForNode;
  onNodeClickRef.current = onNodeClick;
  onEdgeClickRef.current = onEdgeClick;

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
    // Parallel array of the original GraphEdge for each surviving simLink so
    // edge click can pass the source object back to the consumer.
    const edgeForLink: GraphEdge[] = [];
    for (const e of curEdges) {
      const src = nodeById.get(e.source);
      const tgt = nodeById.get(e.target);
      if (!src || !tgt) continue;
      simLinks.push({ source: src, target: tgt, weight: e.weight });
      edgeForLink.push(e);
    }

    // Create DOM elements. For each edge we render TWO lines:
    //   - visual line (1px, follows the simulation tick)
    //   - hit line   (10px transparent, same coordinates, pointer-events:stroke)
    // The hit line is a separate <line> stacked on top of the visual one so
    // users get a generous click target without having to land precisely on
    // the 1px stroke. Both lines update together inside writeDom.
    const lines: SVGLineElement[] = [];
    const hitLines: SVGLineElement[] = [];
    const lineToEdge = new Map<SVGLineElement, GraphEdge>();
    for (let i = 0; i < simLinks.length; i++) {
      const line = document.createElementNS(SVG_NS, "line");
      line.setAttribute("stroke-width", "1");
      line.setAttribute("pointer-events", "none");
      edgeGroup.appendChild(line);
      lines.push(line);

      const hitLine = document.createElementNS(SVG_NS, "line");
      hitLine.setAttribute("stroke", "transparent");
      hitLine.setAttribute("stroke-width", "10");
      hitLine.setAttribute("pointer-events", "stroke");
      if (onEdgeClickRef.current) {
        hitLine.setAttribute("cursor", "pointer");
      }
      edgeGroup.appendChild(hitLine);
      hitLines.push(hitLine);
      lineToEdge.set(hitLine, edgeForLink[i]);
    }

    const circles: SVGCircleElement[] = [];
    const circleToNode = new Map<SVGCircleElement, SimNode>();
    // Cursor stays "grab" — node click is discovered after pointerup, the
    // node is still draggable, and showing "pointer" everywhere would lie
    // about the dominant interaction.
    for (const node of simNodes) {
      const circle = document.createElementNS(SVG_NS, "circle");
      circle.setAttribute("r", "6");
      circle.setAttribute("fill", colorRef.current(node));
      circle.setAttribute("stroke", "hsl(var(--background))");
      circle.setAttribute("stroke-width", "1.5");
      circle.setAttribute("tabindex", "0");
      circle.setAttribute("role", "img");
      circle.setAttribute("aria-label", node.summary || node.id);
      circle.setAttribute("cursor", "grab");
      nodeGroup.appendChild(circle);
      circles.push(circle);
      circleToNode.set(circle, node);
    }

    // Hover + focus listeners
    const handleEnter = (ev: Event) => {
      const n = circleToNode.get(ev.currentTarget as SVGCircleElement);
      if (n) onHoverRef.current(n);
    };
    const handleLeave = () => onHoverRef.current(null);
    const handleFocus = (ev: Event) => {
      const circle = ev.currentTarget as SVGCircleElement;
      circle.setAttribute("stroke", "hsl(var(--ring))");
      circle.setAttribute("stroke-width", "3");
      handleEnter(ev);
    };
    const handleBlur = (ev: Event) => {
      const circle = ev.currentTarget as SVGCircleElement;
      circle.setAttribute("stroke", "hsl(var(--background))");
      circle.setAttribute("stroke-width", "1.5");
      handleLeave();
    };
    // Issue #435: keyboard parity (WCAG 2.1.1). Enter/Space on a focused
    // circle fires the same callback as a mouse click. Drag discrimination
    // does not apply here — keyboard activation is unambiguous.
    const handleKeyDown = (ev: Event) => {
      const ke = ev as KeyboardEvent;
      if (ke.key !== "Enter" && ke.key !== " ") return;
      const n = circleToNode.get(ev.currentTarget as SVGCircleElement);
      if (!n) return;
      ke.preventDefault();
      onNodeClickRef.current?.(n);
    };
    // Issue #435: edge click (only fires when the consumer wired onEdgeClick).
    // The hit line carries the GraphEdge via lineToEdge; the visual line has
    // pointer-events:none so this listener is the single source of truth.
    const handleEdgeClick = (ev: Event) => {
      const me = ev as MouseEvent;
      const edge = lineToEdge.get(ev.currentTarget as SVGLineElement);
      if (!edge) return;
      onEdgeClickRef.current?.(edge, me.clientX, me.clientY);
    };
    const attachListeners = () => {
      for (const c of circles) {
        c.addEventListener("mouseenter", handleEnter);
        c.addEventListener("mouseleave", handleLeave);
        c.addEventListener("focus", handleFocus);
        c.addEventListener("blur", handleBlur);
        c.addEventListener("keydown", handleKeyDown);
      }
      for (const hl of hitLines) {
        hl.addEventListener("click", handleEdgeClick);
      }
    };
    const detachListeners = () => {
      for (const c of circles) {
        c.removeEventListener("mouseenter", handleEnter);
        c.removeEventListener("mouseleave", handleLeave);
        c.removeEventListener("focus", handleFocus);
        c.removeEventListener("blur", handleBlur);
        c.removeEventListener("keydown", handleKeyDown);
      }
      for (const hl of hitLines) {
        hl.removeEventListener("click", handleEdgeClick);
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
        const x1 = String(s.x ?? 0);
        const y1 = String(s.y ?? 0);
        const x2 = String(t.x ?? 0);
        const y2 = String(t.y ?? 0);
        lines[i].setAttribute("x1", x1);
        lines[i].setAttribute("y1", y1);
        lines[i].setAttribute("x2", x2);
        lines[i].setAttribute("y2", y2);
        hitLines[i].setAttribute("x1", x1);
        hitLines[i].setAttribute("y1", y1);
        hitLines[i].setAttribute("x2", x2);
        hitLines[i].setAttribute("y2", y2);
      }
    };

    // Reduced-motion: batch ticks synchronously, draw once, freeze (no drag/zoom).
    // Click support still works — keydown / mouseenter / hit-line click
    // listeners are wired by attachListeners() above and remain active.
    // Node click via mouse needs a dedicated "click" listener here because
    // d3-drag is NOT attached in this branch (so the drag-end click bridge
    // below doesn't run).
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
      const handleStaticNodeClick = (ev: Event) => {
        const n = circleToNode.get(ev.currentTarget as SVGCircleElement);
        if (n) onNodeClickRef.current?.(n);
      };
      for (const c of circles) {
        c.addEventListener("click", handleStaticNodeClick);
      }
      return () => {
        for (const c of circles) {
          c.removeEventListener("click", handleStaticNodeClick);
        }
        detachListeners();
        simulation.stop();
      };
    }

    // --- Zoom & Pan (d3-zoom) ---
    const svgSelection = select(svg);
    const rootSelection = select(rootGroup);

    // Reset any stale __zoom transform from a previous effect run
    svgSelection.call(zoom<SVGSVGElement, unknown>().transform, zoomIdentity);

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

    // ``event.sourceEvent.currentTarget`` is typed ``EventTarget | null``,
    // and the ``?.setAttribute`` chain only short-circuits on null/undefined.
    // On Next.js 16 + React 19 / Turbopack the value is sometimes a non-
    // Element (Window or a stale target after the pointer event lifecycle
    // resets), which lacks ``setAttribute`` and crashes the drag handler.
    // Issue #444. Narrowing via ``instanceof Element`` is the minimal safe
    // guard that preserves the cursor visual on real Elements.
    const setCursor = (target: EventTarget | null, cursor: string) => {
      if (target instanceof Element) {
        target.setAttribute("cursor", cursor);
      }
    };

    // Issue #435: drag/click discrimination is done INSIDE d3-drag rather
    // than via separate onPointerDown/Up — d3-drag captures the pointer on
    // start, so listeners stacked on top are unreliable. We track the start
    // position in `start`, accumulate distance in `drag`, and in `end` fire
    // onNodeClick only if the gesture stayed within a 5px radius
    // (dx*dx + dy*dy < 25). Anchored to event.x/event.y (zoom-corrected
    // SVG-local coords) so the threshold behaves the same at every zoom level.
    let dragStartX = 0;
    let dragStartY = 0;
    let dragMoved = false;
    const DRAG_CLICK_THRESHOLD_SQ = 25; // 5px

    const dragBehavior = drag<SVGCircleElement, SimNode>()
      .on("start", (event: DragEvent) => {
        if (!event.active) simulation.alphaTarget(0.3).restart();
        const d = event.subject;
        d.fx = d.x;
        d.fy = d.y;
        dragStartX = event.x;
        dragStartY = event.y;
        dragMoved = false;
        setCursor(event.sourceEvent.currentTarget, "grabbing");
      })
      .on("drag", (event: DragEvent) => {
        const d = event.subject;
        d.fx = event.x;
        d.fy = event.y;
        if (!dragMoved) {
          const dx = event.x - dragStartX;
          const dy = event.y - dragStartY;
          if (dx * dx + dy * dy >= DRAG_CLICK_THRESHOLD_SQ) {
            dragMoved = true;
          }
        }
      })
      .on("end", (event: DragEvent) => {
        if (!event.active) simulation.alphaTarget(0);
        const d = event.subject;
        d.fx = null;
        d.fy = null;
        setCursor(event.sourceEvent.currentTarget, "grab");
        if (!dragMoved) {
          onNodeClickRef.current?.(d);
        }
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
