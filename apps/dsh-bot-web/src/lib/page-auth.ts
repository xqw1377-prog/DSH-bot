import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { requireViewer } from "@/lib/identity";
import { capabilitiesFrom } from "@/lib/console-view";
import type { Principal } from "@/lib/identity";

export { capabilitiesFrom };
export type { ConsoleCapabilities } from "@/lib/console-view";

/** RSC：先验 IAP Viewer，再由服务端读 Projection。 */
export async function requirePageViewer(): Promise<Principal> {
  const incoming = await headers();
  const request = new Request("http://dsh.local/rsc", { headers: incoming });
  const result = await requireViewer(request);
  if ("error" in result) {
    if (result.error.status === 503) {
      redirect("/identity-unavailable");
    }
    redirect("/unauthenticated");
  }
  return result.principal;
}
