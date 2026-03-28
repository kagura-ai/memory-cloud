"use client";

/**
 * Individual Context Neural Memory Graph Page
 *
 * Shows graph for a specific context (not necessarily the current one).
 * Allows viewing graph without switching the current context.
 */

import { useState, useEffect, useCallback } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { SpinnerLoading } from "@/components/common/LoadingState";
import { NeuralMemoryGraph } from "@/components/graph/NeuralMemoryGraph";
import { GraphControls } from "@/components/graph/GraphControls";
import { NodeDetailsPanel } from "@/components/graph/NodeDetailsPanel";
import { graphApi } from "@/lib/api/graph";
import { useMemoryContext } from "@/contexts/MemoryContextContext";
import { ChevronRight, BarChart3, RefreshCw, Check, Lock, Users } from "lucide-react";
import { Button } from "@/components/ui/button";
import { getContext } from "@/lib/api/contexts";
import type { Context } from "@/lib/types/context";
import type {
  GraphData,
  GraphFilters,
  GraphNode,
  LayoutOptions,
} from "@/lib/types/graph";

export default function ContextGraphPage() {
  const params = useParams();
  const contextId = params.id as string;

  const { currentContext } = useMemoryContext();
  const [context, setContext] = useState<Context | null>(null);
  const [loadingContext, setLoadingContext] = useState(true);
  const [contextError, setContextError] = useState<string | null>(null);

  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filters and layout state
  const [filters, setFilters] = useState<GraphFilters>({
    limit_nodes: 100,
    min_weight: 0.0,
  });
  const [layoutOptions, setLayoutOptions] = useState<LayoutOptions>({
    layout: "force",
    direction: "TB",
  });

  // Fetch context info
  useEffect(() => {
    const fetchContext = async () => {
      try {
        setLoadingContext(true);
        setContextError(null);
        const ctx = await getContext(contextId);
        setContext(ctx);
      } catch (err) {
        console.error('Failed to fetch context:', err);
        setContextError('Failed to load context');
      } finally {
        setLoadingContext(false);
      }
    };

    if (contextId) {
      fetchContext();
    }
  }, [contextId]);

  // Fetch graph data
  const fetchGraphData = useCallback(async () => {
    setIsLoading(true);
    setError(null);

    try {
      // TODO: Update graphApi to accept contextId parameter
      // For now, this will use the current context from the session
      const data = await graphApi.getGraphData(filters);
      setGraphData(data);
    } catch (err) {
      console.error("Failed to fetch graph data:", err);
      setError(
        err instanceof Error ? err.message : "Failed to load graph data"
      );
    } finally {
      setIsLoading(false);
    }
  }, [filters]);

  // Initial load and re-fetch on context change
  useEffect(() => {
    if (contextId) {
      fetchGraphData();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contextId]);

  // Handle filter changes
  const handleFiltersChange = (newFilters: GraphFilters) => {
    setFilters(newFilters);
  };

  // Handle layout changes
  const handleLayoutChange = (newLayout: LayoutOptions) => {
    setLayoutOptions(newLayout);
  };

  // Handle node click
  const handleNodeClick = (node: GraphNode) => {
    setSelectedNode(node);
  };

  // Handle refresh
  const handleRefresh = () => {
    fetchGraphData();
  };

  if (loadingContext) {
    return (
      <PageContainer>
        <SpinnerLoading size="lg" message="Loading context..." />
      </PageContainer>
    );
  }

  if (contextError || !context) {
    return (
      <PageContainer>
        <div className="text-center py-12">
          <p className="text-red-600">{contextError || 'Context not found'}</p>
          <Link href="/workspace/contexts">
            <Button variant="outline" className="mt-4">
              Back to Contexts
            </Button>
          </Link>
        </div>
      </PageContainer>
    );
  }

  const isCurrent = currentContext?.id === context.id;
  const displayName = context.display_name || context.name;
  const pageTitle = `${displayName} - Neural Memory Graph`;

  // Privacy indicator
  const privacyIcon = context.is_private ? (
    <Lock className="h-5 w-5 text-gray-400 inline-block mr-2" aria-label="Private context" />
  ) : (
    <Users className="h-5 w-5 text-blue-500 inline-block mr-2" aria-label="Shared context" />
  );

  if (isLoading && !graphData) {
    return (
      <PageContainer>
        {/* Breadcrumb */}
        <nav className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 mb-4">
          <Link href="/workspace/contexts" className="hover:text-gray-900 dark:hover:text-gray-200 hover:underline">
            Contexts
          </Link>
          <ChevronRight className="h-4 w-4" />
          <div className="flex items-center gap-2">
            <span className="text-gray-900 dark:text-gray-100">{displayName}</span>
            {isCurrent && (
              <span className="px-2 py-0.5 text-xs bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300 rounded-full font-medium flex items-center gap-1">
                <Check className="h-3 w-3" />
                Current
              </span>
            )}
          </div>
        </nav>

        <PageHeader
          title={
            <div className="flex items-center gap-2">
              {privacyIcon}
              <span>{pageTitle}</span>
            </div>
          }
          description="Interactive visualization of memory connections"
          actions={
            <div className="flex items-center gap-2">
              <Link href={`/contexts/${contextId}/stats`}>
                <Button variant="outline" size="sm">
                  <BarChart3 className="h-4 w-4 mr-2" />
                  View Stats
                </Button>
              </Link>
              <Button onClick={handleRefresh} variant="outline" size="sm" disabled={isLoading}>
                <RefreshCw className={`h-4 w-4 mr-2 ${isLoading ? 'animate-spin' : ''}`} />
                Refresh
              </Button>
            </div>
          }
        />
        <SpinnerLoading size="lg" message="Loading graph data..." />
      </PageContainer>
    );
  }

  if (error && !graphData) {
    return (
      <PageContainer>
        {/* Breadcrumb */}
        <nav className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 mb-4">
          <Link href="/workspace/contexts" className="hover:text-gray-900 dark:hover:text-gray-200 hover:underline">
            Contexts
          </Link>
          <ChevronRight className="h-4 w-4" />
          <div className="flex items-center gap-2">
            <span className="text-gray-900 dark:text-gray-100">{displayName}</span>
            {isCurrent && (
              <span className="px-2 py-0.5 text-xs bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300 rounded-full font-medium flex items-center gap-1">
                <Check className="h-3 w-3" />
                Current
              </span>
            )}
          </div>
        </nav>

        <PageHeader
          title={
            <div className="flex items-center gap-2">
              {privacyIcon}
              <span>{pageTitle}</span>
            </div>
          }
          description="Interactive visualization of memory connections"
          actions={
            <div className="flex items-center gap-2">
              <Link href={`/contexts/${contextId}/stats`}>
                <Button variant="outline" size="sm">
                  <BarChart3 className="h-4 w-4 mr-2" />
                  View Stats
                </Button>
              </Link>
              <Button onClick={handleRefresh} variant="outline" size="sm" disabled={isLoading}>
                <RefreshCw className={`h-4 w-4 mr-2 ${isLoading ? 'animate-spin' : ''}`} />
                Refresh
              </Button>
            </div>
          }
        />
        <div className="rounded-lg border-2 border-dashed border-slate-700 p-12">
          <div className="text-center">
            <div className="text-4xl mb-4">⚠️</div>
            <h3 className="text-lg font-semibold text-slate-200 mb-2">
              Failed to Load Graph
            </h3>
            <p className="text-sm text-slate-400 mb-4">{error}</p>
            <button
              onClick={handleRefresh}
              className="px-4 py-2 bg-brand-green hover:bg-brand-green/80 text-white rounded transition-colors"
            >
              Try Again
            </button>
          </div>
        </div>
      </PageContainer>
    );
  }

  // Empty state
  if (graphData && graphData.nodes.length === 0) {
    return (
      <PageContainer>
        {/* Breadcrumb */}
        <nav className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 mb-4">
          <Link href="/workspace/contexts" className="hover:text-gray-900 dark:hover:text-gray-200 hover:underline">
            Contexts
          </Link>
          <ChevronRight className="h-4 w-4" />
          <div className="flex items-center gap-2">
            <span className="text-gray-900 dark:text-gray-100">{displayName}</span>
            {isCurrent && (
              <span className="px-2 py-0.5 text-xs bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300 rounded-full font-medium flex items-center gap-1">
                <Check className="h-3 w-3" />
                Current
              </span>
            )}
          </div>
        </nav>

        <PageHeader
          title={
            <div className="flex items-center gap-2">
              {privacyIcon}
              <span>{pageTitle}</span>
            </div>
          }
          description="Interactive visualization of memory connections"
          actions={
            <div className="flex items-center gap-2">
              <Link href={`/contexts/${contextId}/stats`}>
                <Button variant="outline" size="sm">
                  <BarChart3 className="h-4 w-4 mr-2" />
                  View Stats
                </Button>
              </Link>
              <Button onClick={handleRefresh} variant="outline" size="sm" disabled={isLoading}>
                <RefreshCw className={`h-4 w-4 mr-2 ${isLoading ? 'animate-spin' : ''}`} />
                Refresh
              </Button>
            </div>
          }
        />
        <div className="rounded-lg border-2 border-dashed border-slate-700 p-12">
          <div className="text-center">
            <div className="text-4xl mb-4">🧠</div>
            <h3 className="text-lg font-semibold text-slate-200 mb-2">
              No Neural Memory Graph Yet
            </h3>
            <p className="text-sm text-slate-400 mb-4">
              Start creating memories to build your neural memory graph. As you
              add more memories, the AI will automatically learn connections
              between them using Hebbian learning.
            </p>
            <Link href={`/contexts/${contextId}/stats`}>
              <button className="px-4 py-2 bg-brand-green hover:bg-brand-green/80 text-white rounded transition-colors">
                View Usage Stats
              </button>
            </Link>
          </div>
        </div>
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      {/* Breadcrumb */}
      <nav className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 mb-4">
        <Link href="/workspace/contexts" className="hover:text-gray-900 dark:hover:text-gray-200 hover:underline">
          Contexts
        </Link>
        <ChevronRight className="h-4 w-4" />
        <div className="flex items-center gap-2">
          <span className="text-gray-900 dark:text-gray-100">{displayName}</span>
          {isCurrent && (
            <span className="px-2 py-0.5 text-xs bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300 rounded-full font-medium flex items-center gap-1">
              <Check className="h-3 w-3" />
              Current
            </span>
          )}
        </div>
      </nav>

      <PageHeader
        title={
          <div className="flex items-center gap-2">
            {privacyIcon}
            <span>{pageTitle}</span>
          </div>
        }
        description={`${graphData?.stats.filtered_nodes || 0} nodes, ${
          graphData?.stats.filtered_edges || 0
        } connections`}
        actions={
          <div className="flex items-center gap-2">
            <Link href={`/contexts/${contextId}/stats`}>
              <Button variant="outline" size="sm">
                <BarChart3 className="h-4 w-4 mr-2" />
                View Stats
              </Button>
            </Link>
            <Button onClick={handleRefresh} variant="outline" size="sm" disabled={isLoading}>
              <RefreshCw className={`h-4 w-4 mr-2 ${isLoading ? 'animate-spin' : ''}`} />
              Refresh
            </Button>
          </div>
        }
      />

      <div className="grid grid-cols-1 lg:grid-cols-[300px_1fr_300px] gap-4 h-[calc(100vh-200px)]">
        {/* Left Sidebar - Controls */}
        <div className="lg:h-full overflow-y-auto">
          <GraphControls
            filters={filters}
            layoutOptions={layoutOptions}
            onFiltersChange={handleFiltersChange}
            onLayoutChange={handleLayoutChange}
            onRefresh={handleRefresh}
            isLoading={isLoading}
          />
        </div>

        {/* Center - Graph Visualization */}
        <div className="bg-slate-900 rounded-lg border border-slate-700 h-full min-h-[500px]">
          {graphData && (
            <NeuralMemoryGraph
              graphData={graphData}
              onNodeClick={handleNodeClick}
              layoutOptions={layoutOptions}
            />
          )}
        </div>

        {/* Right Sidebar - Node Details */}
        <div className="lg:h-full overflow-y-auto">
          <NodeDetailsPanel
            selectedNode={selectedNode}
            onClose={() => setSelectedNode(null)}
          />
        </div>
      </div>
    </PageContainer>
  );
}
