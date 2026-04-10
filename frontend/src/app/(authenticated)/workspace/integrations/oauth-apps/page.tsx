import { redirect } from "next/navigation";

export default function OAuthAppsRedirect() {
  redirect("/workspace/integrations/credentials?tab=oauth-apps");
}
