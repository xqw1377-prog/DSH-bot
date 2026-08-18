import { MarketDrilldown } from "@/components/market-drilldown";
import { requirePageViewer } from "@/lib/page-auth";

export const dynamic = "force-dynamic";

export default async function CryptoPage() {
  await requirePageViewer();
  return <MarketDrilldown market="CRYPTO" title="Crypto Bot" bot="crypto-bot" />;
}
