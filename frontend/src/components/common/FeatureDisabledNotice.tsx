import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { Alert, AlertDescription } from "@/components/ui/alert";

/**
 * Notice shown when a deployment feature flag (e.g. `byok`) is off and the user
 * reached a gated page by direct navigation (the sidebar entry is hidden).
 *
 * v0.42 review #22: extracts the PageContainer + PageHeader + Alert boilerplate
 * that the external-keys and cost dashboards hand-rolled identically. The
 * loading states stay page-specific (skeleton vs spinner) per the shape-driven
 * LoadingState rule, so only this disabled notice is shared.
 */
export function FeatureDisabledNotice({
  title,
  description,
  message,
}: {
  title: string;
  description?: string;
  message: string;
}) {
  return (
    <PageContainer>
      <PageHeader title={title} description={description} />
      <Alert>
        <AlertDescription>{message}</AlertDescription>
      </Alert>
    </PageContainer>
  );
}
