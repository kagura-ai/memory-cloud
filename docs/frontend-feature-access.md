# Discovering features in the web UI

A signed-in user can follow a feature link without having permission to use the feature. The route shows its purpose, selected workspace and required role instead of fetching restricted data or sending the user to the dashboard. Authorization remains on the API. Do not use private IDs, object names, counts or samples from protected APIs to explain a restriction.

| Destination | Page/data boundary | Plan or deployment behavior |
|---|---|---|
| Dashboard | Workspace member or above; viewers get an explanation, not statistics | Never sell an upgrade for a role requirement |
| Contexts | List and overview respect existing context access; ordinary tabs can be opened, admin panels mount only for admin/owner | Analyses remains subject to its workspace allowlist and owner requirement |
| Resources (including detail) | Workspace owner | Lower tiers can read authorized existing resources; explain the creation limit |
| Connectors | Workspace admin or owner | Preserve existing connectors after downgrade; explain the creation limit |
| Members, secrets and sleep reports (including detail) | Workspace admin or owner | A plan, capacity or allowlist refusal is distinct from a missing role |
| Storage, API keys and custom OAuth apps | Retain existing per-object and per-action checks | Do not reveal another user's records |
| Plan and workspace settings | Only the selected workspace owner can manage them | Billing must be enabled for an actionable upgrade link; workspace creation remains available separately |
| External provider keys and workspace cost | Owner for keys, admin/owner for cost | Respect deployment flags. The owner can still manage existing external keys when provisioning is disabled |
| System administration, including unlisted direct/detail routes | System admin, independent of workspace role | An upgrade does not confer system privileges |

For mixed restrictions, use the tier matrix and the role of the current workspace. A billing-capable owner can compare plans; other roles should be directed to their workspace owner when a plan change would help. Pending checks, request errors and a disabled deployment are not plan restrictions. Keep explanations in the page and make them accessible to keyboard and touch users.

The sidebar catalog and route boundary use the same role metadata. When adding a restricted route, cover both navigation and a direct URL; assert that no protected fetch occurs in the denied or switching state. Keep sensitive nested panels unmounted even when their descriptive tabs are visible.
