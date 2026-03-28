"use client";

/**
 * Graph Controls Component
 * Issue #60 - Filtering and layout controls for Neural Memory Graph
 */

import { useState } from "react";
import { InlineSpinner } from "@/components/common/LoadingState";
import type { GraphFilters, GraphLayout, LayoutOptions } from "@/lib/types/graph";
import { MEMORY_TYPE_COLORS } from "@/lib/types/graph";

interface GraphControlsProps {
  filters: GraphFilters;
  layoutOptions: LayoutOptions;
  onFiltersChange: (filters: GraphFilters) => void;
  onLayoutChange: (layout: LayoutOptions) => void;
  onRefresh: () => void;
  isLoading?: boolean;
}

const MEMORY_TYPES = Object.keys(MEMORY_TYPE_COLORS).filter((t) => t !== "unknown");

export function GraphControls({
  filters,
  layoutOptions,
  onFiltersChange,
  onLayoutChange,
  onRefresh,
  isLoading = false,
}: GraphControlsProps) {
  const [isExpanded, setIsExpanded] = useState(true);

  const handleLimitChange = (value: number) => {
    onFiltersChange({ ...filters, limit_nodes: value });
  };

  const handleWeightChange = (value: number) => {
    onFiltersChange({ ...filters, min_weight: value });
  };

  const handleTypeToggle = (type: string) => {
    const currentTypes = filters.memory_types || [];
    const newTypes = currentTypes.includes(type)
      ? currentTypes.filter((t) => t !== type)
      : [...currentTypes, type];

    onFiltersChange({
      ...filters,
      memory_types: newTypes.length > 0 ? newTypes : undefined,
    });
  };

  const handleLayoutChange = (layout: GraphLayout) => {
    onLayoutChange({ ...layoutOptions, layout });
  };

  const handleDirectionChange = (
    direction: "TB" | "LR" | "BT" | "RL"
  ) => {
    onLayoutChange({ ...layoutOptions, direction });
  };

  return (
    <div className="bg-slate-800 rounded-lg border border-slate-700 overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between p-4 border-b border-slate-700">
        <h3 className="text-sm font-semibold text-slate-200">Graph Controls</h3>
        <div className="flex items-center gap-2">
          <button
            onClick={onRefresh}
            disabled={isLoading}
            className="px-3 py-1 text-xs bg-brand-green hover:bg-brand-green/80 text-white rounded disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-2"
          >
            {isLoading && <InlineSpinner size="xs" variant="default" />}
            {isLoading ? "Loading..." : "Refresh"}
          </button>
          <button
            onClick={() => setIsExpanded(!isExpanded)}
            className="p-1 text-slate-400 hover:text-slate-200 transition-colors"
          >
            {isExpanded ? "▼" : "▶"}
          </button>
        </div>
      </div>

      {isExpanded && (
        <div className="p-4 space-y-6">
          {/* Layout */}
          <div>
            <label className="block text-xs font-medium text-slate-300 mb-2">
              Layout Algorithm
            </label>
            <div className="grid grid-cols-3 gap-2">
              {(["force", "dagre", "circular"] as GraphLayout[]).map((layout) => {
                const label = layout === "dagre" ? "Grid" : layout.charAt(0).toUpperCase() + layout.slice(1);
                return (
                  <button
                    key={layout}
                    onClick={() => handleLayoutChange(layout)}
                    className={`px-3 py-2 text-xs rounded transition-colors ${
                      layoutOptions.layout === layout
                        ? "bg-brand-green text-white"
                        : "bg-slate-700 text-slate-300 hover:bg-slate-600"
                    }`}
                  >
                    {label}
                  </button>
                );
              })}
            </div>

            {/* Direction for dagre layout */}
            {layoutOptions.layout === "dagre" && (
              <div className="mt-3">
                <label className="block text-xs font-medium text-slate-300 mb-2">
                  Direction
                </label>
                <div className="grid grid-cols-4 gap-2">
                  {(["TB", "LR", "BT", "RL"] as const).map((dir) => (
                    <button
                      key={dir}
                      onClick={() => handleDirectionChange(dir)}
                      className={`px-2 py-1 text-xs rounded transition-colors ${
                        layoutOptions.direction === dir
                          ? "bg-brand-green text-white"
                          : "bg-slate-700 text-slate-300 hover:bg-slate-600"
                      }`}
                    >
                      {dir}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Node Limit */}
          <div>
            <label className="block text-xs font-medium text-slate-300 mb-2">
              Max Nodes: {filters.limit_nodes || 100}
            </label>
            <input
              type="range"
              min="10"
              max="500"
              step="10"
              value={filters.limit_nodes || 100}
              onChange={(e) => handleLimitChange(Number(e.target.value))}
              className="w-full h-2 bg-slate-700 rounded-lg appearance-none cursor-pointer accent-brand-green"
            />
            <div className="flex justify-between text-xs text-slate-500 mt-1">
              <span>10</span>
              <span>500</span>
            </div>
          </div>

          {/* Edge Weight Threshold */}
          <div>
            <label className="block text-xs font-medium text-slate-300 mb-2">
              Min Edge Weight: {(filters.min_weight ?? 0.0).toFixed(2)}
            </label>
            <input
              type="range"
              min="0"
              max="3"
              step="0.1"
              value={filters.min_weight ?? 0.0}
              onChange={(e) => handleWeightChange(Number(e.target.value))}
              className="w-full h-2 bg-slate-700 rounded-lg appearance-none cursor-pointer accent-brand-green"
            />
            <div className="flex justify-between text-xs text-slate-500 mt-1">
              <span>0.0</span>
              <span>3.0</span>
            </div>
          </div>

          {/* Memory Types Filter */}
          <div>
            <label className="block text-xs font-medium text-slate-300 mb-2">
              Memory Types
            </label>
            <div className="grid grid-cols-2 gap-2">
              {MEMORY_TYPES.map((type) => {
                const isSelected =
                  !filters.memory_types || filters.memory_types.includes(type);
                const color = MEMORY_TYPE_COLORS[type];

                return (
                  <button
                    key={type}
                    onClick={() => handleTypeToggle(type)}
                    className={`flex items-center gap-2 px-3 py-2 rounded text-xs transition-all ${
                      isSelected
                        ? "bg-slate-700 opacity-100"
                        : "bg-slate-800 opacity-40"
                    }`}
                  >
                    <div
                      className="w-3 h-3 rounded-full"
                      style={{ backgroundColor: color }}
                    />
                    <span className="text-slate-300 capitalize">{type}</span>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Clear Filters */}
          <button
            onClick={() =>
              onFiltersChange({
                limit_nodes: 100,
                min_weight: 0.0,
                memory_types: undefined,
              })
            }
            className="w-full px-3 py-2 text-xs bg-slate-700 hover:bg-slate-600 text-slate-300 rounded transition-colors"
          >
            Reset Filters
          </button>
        </div>
      )}
    </div>
  );
}
