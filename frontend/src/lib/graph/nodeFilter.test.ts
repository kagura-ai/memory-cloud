import { describe, it, expect } from "vitest";
import { applyFilter, type FilterStrategy } from "./nodeFilter";
import type { GraphNode, GraphEdge } from "@/lib/types/graph";

function makeNode(id: string, overrides?: Partial<GraphNode>): GraphNode {
  return {
    id,
    summary: `Node ${id}`,
    type: "note",
    importance: 0.5,
    degree: 1,
    ...overrides,
  };
}

function makeEdge(source: string, target: string, weight = 0.5): GraphEdge {
  return { source, target, weight, type: "neural_association" };
}

const nodes: GraphNode[] = [
  makeNode("a", { degree: 5, importance: 0.3 }),
  makeNode("b", { degree: 3, importance: 0.9 }),
  makeNode("c", { degree: 1, importance: 0.7 }),
  makeNode("d", { degree: 5, importance: 0.1 }),
  makeNode("e", { degree: 2, importance: 0.5 }),
];

const edges: GraphEdge[] = [
  makeEdge("a", "b", 1.0),
  makeEdge("a", "c", 0.5),
  makeEdge("b", "c", 0.3),
  makeEdge("d", "e", 0.8),
  makeEdge("a", "d", 0.2),
];

describe("applyFilter", () => {
  describe("degree strategy", () => {
    it("selects top-N nodes by degree", () => {
      const result = applyFilter({ nodes, edges, n: 2, strategy: "degree" });
      expect(result.nodes.map((n) => n.id)).toEqual(["a", "d"]);
    });

    it("tie-breaks by node id (lexicographic)", () => {
      // a and d both have degree 5 — 'a' < 'd'
      const result = applyFilter({ nodes, edges, n: 3, strategy: "degree" });
      expect(result.nodes.map((n) => n.id)).toEqual(["a", "d", "b"]);
    });
  });

  describe("importance strategy", () => {
    it("selects top-N nodes by importance", () => {
      const result = applyFilter({
        nodes,
        edges,
        n: 3,
        strategy: "importance",
      });
      expect(result.nodes.map((n) => n.id)).toEqual(["b", "c", "e"]);
    });
  });

  describe("weightSum strategy", () => {
    it("selects top-N by sum of adjacent edge weights", () => {
      // a: 1.0 + 0.5 + 0.2 = 1.7
      // b: 1.0 + 0.3 = 1.3
      // c: 0.5 + 0.3 = 0.8
      // d: 0.8 + 0.2 = 1.0
      // e: 0.8
      const result = applyFilter({
        nodes,
        edges,
        n: 3,
        strategy: "weightSum",
      });
      expect(result.nodes.map((n) => n.id)).toEqual(["a", "b", "d"]);
    });
  });

  describe("induced edge subgraph", () => {
    it("keeps only edges where both endpoints survived", () => {
      const result = applyFilter({ nodes, edges, n: 2, strategy: "degree" });
      // nodes: a, d — only edge a→d survives
      expect(result.edges).toHaveLength(1);
      expect(result.edges[0].source).toBe("a");
      expect(result.edges[0].target).toBe("d");
    });

    it("returns empty edges when no pair survives", () => {
      const result = applyFilter({
        nodes,
        edges,
        n: 1,
        strategy: "importance",
      });
      // only node b — no edges have both endpoints = b
      expect(result.nodes.map((n) => n.id)).toEqual(["b"]);
      expect(result.edges).toHaveLength(0);
    });
  });

  describe("edge cases", () => {
    it("returns all nodes when n >= node count", () => {
      const result = applyFilter({ nodes, edges, n: 100, strategy: "degree" });
      expect(result.nodes).toHaveLength(5);
      expect(result.edges).toHaveLength(5);
    });

    it("handles empty input", () => {
      const result = applyFilter({
        nodes: [],
        edges: [],
        n: 10,
        strategy: "degree",
      });
      expect(result.nodes).toHaveLength(0);
      expect(result.edges).toHaveLength(0);
    });

    it("handles nodes with missing degree", () => {
      const nodesNoDegree = [
        makeNode("x", { degree: undefined as unknown as number }),
        makeNode("y", { degree: 3 }),
      ];
      const result = applyFilter({
        nodes: nodesNoDegree,
        edges: [],
        n: 2,
        strategy: "degree",
      });
      expect(result.nodes[0].id).toBe("y");
      expect(result.nodes[1].id).toBe("x");
    });
  });
});
