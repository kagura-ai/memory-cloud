import { redirect } from "next/navigation";

/**
 * Redirect stub — consolidated into /workspace/contexts/[id]?tab=connections (#232)
 */
export default async function GraphRedirect({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  redirect(`/workspace/contexts/${id}?tab=connections`);
}
