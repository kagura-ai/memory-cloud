import { redirect } from "next/navigation";

export default function ApiKeysRedirect() {
  redirect("/workspace/integrations/credentials?tab=api-keys");
}
