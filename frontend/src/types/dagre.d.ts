/**
 * Type declarations for @dagrejs/dagre
 * Issue #60 - Neural Memory Graph Visualization
 */

declare module "@dagrejs/dagre" {
  export namespace graphlib {
    class Graph {
      constructor(options?: any);
      setGraph(options: any): void;
      setDefaultEdgeLabel(callback: () => any): void;
      setNode(id: string, options: any): void;
      setEdge(source: string, target: string): void;
      node(id: string): any;
      nodes(): string[];
      edges(): Array<{ v: string; w: string }>;
    }
  }

  export function layout(graph: graphlib.Graph): void;

  const dagre: {
    graphlib: typeof graphlib;
    layout: typeof layout;
  };

  export default dagre;
}
