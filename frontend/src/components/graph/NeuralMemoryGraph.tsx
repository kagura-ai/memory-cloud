"use client";

/**
 * Neural Memory Graph Visualization Component
 * Issue #60 - Interactive node/edge display using React Flow
 */

import { useCallback, useMemo } from "react";
import ReactFlow, {
  Node,
  Edge,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  Connection,
  ConnectionMode,
  Panel,
} from "reactflow";
import "reactflow/dist/style.css";

import type {
  GraphNode as GraphNodeData,
  GraphEdge as GraphEdgeData,
  GraphLayout,
  LayoutOptions,
} from "@/lib/types/graph";
import { getMemoryTypeColor } from "@/lib/types/graph";

interface NeuralMemoryGraphProps {
  graphData: { nodes: GraphNodeData[]; edges: GraphEdgeData[] };
  onNodeClick?: (node: GraphNodeData) => void;
  layoutOptions?: LayoutOptions;
}

// Convert backend graph data to React Flow format
function convertToReactFlowNodes(
  backendNodes: GraphNodeData[],
  layoutOptions: LayoutOptions
): Node[] {
  const nodes: Node[] = backendNodes.map((node) => ({
    id: node.id,
    type: "default",
    data: {
      label: node.summary.length > 50 ? `${node.summary.slice(0, 47)}...` : node.summary,
      ...node,
    },
    position: { x: 0, y: 0 }, // Will be calculated by layout
    style: {
      background: getMemoryTypeColor(node.type),
      color: "#ffffff",
      border: "1px solid rgba(255, 255, 255, 0.2)",
      borderRadius: "8px",
      padding: "10px",
      width: 150 + node.importance * 100, // Size based on importance
      fontSize: "12px",
      fontWeight: 500,
    },
  }));

  // Apply layout
  return applyLayout(nodes, [], layoutOptions);
}

function convertToReactFlowEdges(backendEdges: GraphEdgeData[]): Edge[] {
  return backendEdges.map((edge) => ({
    id: `${edge.source}-${edge.target}`,
    source: edge.source,
    target: edge.target,
    type: "smoothstep",
    animated: edge.weight > 2.0, // Animate strong connections
    style: {
      stroke: `rgba(148, 163, 184, ${0.3 + edge.weight * 0.3})`, // Opacity based on weight
      strokeWidth: 1 + edge.weight * 2, // Thickness based on weight
    },
    label: edge.weight > 2.0 ? `${edge.weight.toFixed(2)}` : undefined,
    labelStyle: {
      fontSize: "10px",
      fill: "#94a3b8",
    },
  }));
}

// Layout algorithms
function applyLayout(
  nodes: Node[],
  edges: Edge[],
  options: LayoutOptions
): Node[] {
  switch (options.layout) {
    case "dagre":
      return applyGridLayout(nodes, options.direction || "TB");
    case "circular":
      return applyCircularLayout(nodes);
    case "force":
    default:
      // Force layout - random initial positions, React Flow handles the rest
      return nodes.map((node, i) => ({
        ...node,
        position: {
          x: Math.random() * 1000,
          y: Math.random() * 800,
        },
      }));
  }
}

// Grid layout (replacing dagre for Turbopack compatibility)
function applyGridLayout(
  nodes: Node[],
  direction: "TB" | "LR" | "BT" | "RL"
): Node[] {
  const cols = Math.ceil(Math.sqrt(nodes.length));
  const horizontalSpacing = 250;
  const verticalSpacing = 150;

  return nodes.map((node, i) => {
    const row = Math.floor(i / cols);
    const col = i % cols;

    let x: number, y: number;

    switch (direction) {
      case "LR": // Left to Right
        x = col * horizontalSpacing;
        y = row * verticalSpacing;
        break;
      case "RL": // Right to Left
        x = (cols - col) * horizontalSpacing;
        y = row * verticalSpacing;
        break;
      case "BT": // Bottom to Top
        x = col * horizontalSpacing;
        y = (Math.ceil(nodes.length / cols) - row) * verticalSpacing;
        break;
      case "TB": // Top to Bottom (default)
      default:
        x = col * horizontalSpacing;
        y = row * verticalSpacing;
    }

    return {
      ...node,
      position: { x, y },
    };
  });
}

// Circular layout
function applyCircularLayout(nodes: Node[]): Node[] {
  const radius = Math.min(400, nodes.length * 30);
  const angleStep = (2 * Math.PI) / nodes.length;

  return nodes.map((node, i) => ({
    ...node,
    position: {
      x: 400 + radius * Math.cos(i * angleStep),
      y: 300 + radius * Math.sin(i * angleStep),
    },
  }));
}

export function NeuralMemoryGraph({
  graphData,
  onNodeClick,
  layoutOptions = { layout: "force" },
}: NeuralMemoryGraphProps) {
  // Convert data
  const initialNodes = useMemo(
    () => convertToReactFlowNodes(graphData.nodes, layoutOptions),
    [graphData.nodes, layoutOptions]
  );
  const initialEdges = useMemo(
    () => convertToReactFlowEdges(graphData.edges),
    [graphData.edges]
  );

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);

  // Update when data or layout changes
  useMemo(() => {
    setNodes(convertToReactFlowNodes(graphData.nodes, layoutOptions));
    setEdges(convertToReactFlowEdges(graphData.edges));
  }, [graphData, layoutOptions, setNodes, setEdges]);

  // Handle node click
  const handleNodeClick = useCallback(
    (_event: React.MouseEvent, node: Node) => {
      if (onNodeClick && node.data) {
        onNodeClick(node.data as GraphNodeData);
      }
    },
    [onNodeClick]
  );

  // No connection handler (read-only graph)
  const onConnect = useCallback((_connection: Connection) => {
    // Read-only graph - no connections allowed
  }, []);

  return (
    <div className="w-full h-full">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={handleNodeClick}
        onConnect={onConnect}
        connectionMode={ConnectionMode.Loose}
        fitView
        attributionPosition="bottom-left"
        minZoom={0.1}
        maxZoom={2}
      >
        <Background color="#1e293b" gap={16} />
        <Controls className="bg-slate-800 border-slate-700" />
        <MiniMap
          className="bg-slate-800 border-slate-700"
          nodeColor={(node) => {
            const nodeData = node.data as GraphNodeData;
            return getMemoryTypeColor(nodeData.type);
          }}
        />
        <Panel position="top-left" className="bg-slate-800/90 p-4 rounded-lg">
          <div className="text-sm text-slate-300">
            <div className="font-semibold mb-2">Neural Memory Graph</div>
            <div className="space-y-1">
              <div>Nodes: {graphData.nodes.length}</div>
              <div>Edges: {graphData.edges.length}</div>
            </div>
          </div>
        </Panel>
      </ReactFlow>
    </div>
  );
}
