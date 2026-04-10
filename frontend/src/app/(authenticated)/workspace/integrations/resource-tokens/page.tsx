import { redirect } from "next/navigation";

export default function ResourceTokensRedirect() {
  redirect("/workspace/integrations/credentials?tab=resource-tokens");
}
