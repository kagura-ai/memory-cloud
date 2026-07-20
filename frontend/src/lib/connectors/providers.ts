import type { LucideIcon } from "lucide-react";
import { MessageCircle, Slack, Users } from "lucide-react";

import { slackInstallUrl } from "@/lib/api/workspace-connectors";

// #1389/#1390: frontend provider descriptor so the connect CTA and row
// rendering stop multiplying Slack-hardcoded JSX. This is deliberately a
// UI-only skeleton — backend routing, callback params, and i18n keys stay
// per-provider until a second provider actually lands (#1390 records the
// generalization policy; renaming those now would be churn with no user
// value).
export type ConnectorProviderKey = "slack" | "discord" | "teams";

export interface ConnectorProviderDescriptor {
  key: ConnectorProviderKey;
  /** Brand name — rendered verbatim, never translated. */
  name: string;
  icon: LucideIcon;
  /** How a new connector of this provider gets created today. */
  connectFlow: "oauth" | "manual";
  /** false → rendered as a disabled "coming soon" affordance. */
  enabled: boolean;
  /**
   * Starts this provider's connect flow. Present iff enabled — routing
   * lives in the descriptor so flipping a provider's `enabled` without
   * wiring its own flow yields a dead button, never another provider's
   * OAuth consent screen.
   */
  installUrl?: () => string;
}

export const CONNECTOR_PROVIDERS: ConnectorProviderDescriptor[] = [
  {
    key: "slack",
    name: "Slack",
    icon: Slack,
    connectFlow: "oauth",
    enabled: true,
    installUrl: slackInstallUrl,
  },
  {
    key: "discord",
    name: "Discord",
    icon: MessageCircle,
    connectFlow: "oauth",
    enabled: false,
  },
  {
    key: "teams",
    name: "Microsoft Teams",
    icon: Users,
    connectFlow: "oauth",
    enabled: false,
  },
];
