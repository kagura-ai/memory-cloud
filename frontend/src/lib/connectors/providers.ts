import type { LucideIcon } from "lucide-react";
import { MessageCircle, Slack, Users } from "lucide-react";

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
}

export const CONNECTOR_PROVIDERS: ConnectorProviderDescriptor[] = [
  {
    key: "slack",
    name: "Slack",
    icon: Slack,
    connectFlow: "oauth",
    enabled: true,
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
