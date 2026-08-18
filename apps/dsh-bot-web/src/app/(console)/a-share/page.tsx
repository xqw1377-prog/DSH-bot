import { MarketDrilldown } from "@/components/market-drilldown";
import { requirePageViewer } from "@/lib/page-auth";

export const dynamic = "force-dynamic";
export const revalidate = 0;
export const fetchCache = "force-no-store";

export default async function ASharePage() {
  await requirePageViewer();
  return <MarketDrilldown market="A_SHARE" title="A 股 Bot" bot="a-stock-bot" />;
}
