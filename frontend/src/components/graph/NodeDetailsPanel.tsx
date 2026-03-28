"use client";

/**
 * Node Details Panel Component
 * Issue #60 - Display selected node details and provide actions
 */

import { useRouter } from "next/navigation";
import type { GraphNode } from "@/lib/types/graph";
import { getMemoryTypeColor } from "@/lib/types/graph";

interface NodeDetailsPanelProps {
  selectedNode: GraphNode | null;
  onClose: () => void;
}

export function NodeDetailsPanel({
  selectedNode,
  onClose,
}: NodeDetailsPanelProps) {
  const router = useRouter();

  if (!selectedNode) {
    return (
      <div className="bg-slate-800 rounded-lg border border-slate-700 p-6 h-full flex items-center justify-center">
        <div className="text-center text-slate-400">
          <div className="text-4xl mb-2">📊</div>
          <p className="text-sm">Click a node to view details</p>
        </div>
      </div>
    );
  }

  const typeColor = getMemoryTypeColor(selectedNode.type);

  return (
    <div className="bg-slate-800 rounded-lg border border-slate-700 overflow-hidden h-full flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between p-4 border-b border-slate-700">
        <h3 className="text-sm font-semibold text-slate-200">Node Details</h3>
        <button
          onClick={onClose}
          className="p-1 text-slate-400 hover:text-slate-200 transition-colors"
          aria-label="Close details panel"
        >
          ✕
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Type Badge */}
        <div className="flex items-center gap-2">
          <div
            className="w-3 h-3 rounded-full"
            style={{ backgroundColor: typeColor }}
          />
          <span className="text-xs font-medium text-slate-300 capitalize">
            {selectedNode.type}
          </span>
        </div>

        {/* Summary */}
        <div>
          <label className="block text-xs font-medium text-slate-400 mb-1">
            Summary
          </label>
          <p className="text-sm text-slate-200 leading-relaxed">
            {selectedNode.summary}
          </p>
        </div>

        {/* Stats */}
        <div className="grid grid-cols-2 gap-3">
          <div className="bg-slate-900/50 rounded-lg p-3">
            <div className="text-xs text-slate-400 mb-1">Importance</div>
            <div className="text-lg font-semibold text-brand-green">
              {(selectedNode.importance * 100).toFixed(0)}%
            </div>
          </div>
          <div className="bg-slate-900/50 rounded-lg p-3">
            <div className="text-xs text-slate-400 mb-1">Connections</div>
            <div className="text-lg font-semibold text-blue-400">
              {selectedNode.degree}
            </div>
          </div>
        </div>

        {/* Created Date */}
        {selectedNode.created_at && (
          <div>
            <label className="block text-xs font-medium text-slate-400 mb-1">
              Created
            </label>
            <p className="text-sm text-slate-300">
              {new Date(selectedNode.created_at).toLocaleString()}
            </p>
          </div>
        )}

        {/* Node ID */}
        <div>
          <label className="block text-xs font-medium text-slate-400 mb-1">
            Memory ID
          </label>
          <code className="block text-xs text-slate-400 bg-slate-900/50 p-2 rounded font-mono break-all">
            {selectedNode.id}
          </code>
        </div>
      </div>

      {/* Actions */}
      <div className="p-4 border-t border-slate-700 space-y-2">
        <button
          onClick={() => router.push(`/memory/${selectedNode.id}`)}
          className="w-full px-4 py-2 text-sm bg-brand-green hover:bg-brand-green/80 text-white rounded transition-colors"
        >
          View Full Details
        </button>
        <button
          onClick={() => {
            // TODO: Implement explore from this node
          }}
          className="w-full px-4 py-2 text-sm bg-slate-700 hover:bg-slate-600 text-slate-300 rounded transition-colors"
        >
          Explore Connections
        </button>
      </div>
    </div>
  );
}
