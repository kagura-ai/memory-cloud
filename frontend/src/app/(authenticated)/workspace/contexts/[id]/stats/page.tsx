import { redirect } from "next/navigation";

/**
 * Redirect stub — consolidated into /workspace/contexts/[id]?tab=overview (#232)
 */
export default async function StatsRedirect({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  redirect(`/workspace/contexts/${id}?tab=overview`);
}
