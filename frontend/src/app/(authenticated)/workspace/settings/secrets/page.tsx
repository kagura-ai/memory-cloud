import { redirect } from "next/navigation";

/**
 * Back-compat redirect: the secret store console moved out of Settings into the
 * Workspace group (it is a workspace-scoped store, alongside resources/storage —
 * not a configuration surface). Old bookmarks / links to the Settings path keep
 * working. New canonical route: /workspace/secrets.
 */
export default function WorkspaceSettingsSecretsRedirect() {
  redirect("/workspace/secrets");
}
