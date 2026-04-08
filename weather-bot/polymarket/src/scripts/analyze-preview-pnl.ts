import fs from "fs";
import path from "path";
import axios from "axios";

const HISTORY_FILE = "/tmp/trade-history.jsonl";
const DATA_API_URL = "https://data-api.polymarket.com";
const CLOB_API_URL = "https://clob.polymarket.com";

interface Trade {
  timestamp: string;
  traderAddress: string;
  market: string;
  side: string;
  copySize: number;
  price: number;
  status: string;
}

interface OutcomeInfo {
  tokenId: string;
  outcome: string;
  price: number;
  winner: boolean;
}

interface MarketData {
  conditionId: string;
  question: string;
  tokens: OutcomeInfo[];
}

interface PositionStats {
  title: string;
  tokenId: string;
  conditionId: string;
  costBasis: number;
  shares: number;
  buys: number;
  avgPrice: number;
  currentPrice: number;
  winner: boolean;
  resolved: boolean;
  pnl: number;
  outcome: string;
}

async function fetchTraderActivity(addr: string): Promise<any[]> {
    try {
        const res = await axios.get(`${DATA_API_URL}/activity`, {
            params: { user: addr, type: "TRADE", limit: 500 }
        });
        return res.data;
    } catch {
        return [];
    }
}

async function fetchClobMarket(conditionId: string): Promise<MarketData | null> {
    try {
        const res = await axios.get(`${CLOB_API_URL}/markets/${conditionId}`);
        return {
            conditionId: res.data.condition_id,
            question: res.data.question,
            tokens: res.data.tokens.map((t: any) => ({
                tokenId: t.token_id,
                outcome: t.outcome,
                price: parseFloat(t.price),
                winner: !!t.winner
            }))
        };
    } catch {
        return null;
    }
}

async function run(): Promise<void> {
    if (!fs.existsSync(HISTORY_FILE)) {
        console.error("Log file not found");
        return;
    }

    const lines = fs.readFileSync(HISTORY_FILE, "utf8").trim().split("\n");
    const previewTrades: Trade[] = [];
    const traders = new Set<string>();

    for (const line of lines) {
        try {
            const t = JSON.parse(line);
            if (t.status === "preview") {
                previewTrades.push(t);
                traders.add(t.traderAddress);
            }
        } catch { /* skip */ }
    }

    console.log(`Analyzing ${previewTrades.length} preview trades from ${traders.size} traders...`);
    
    // Step 1: Fetch trader activity
    const activityByTrader: Record<string, any[]> = {};
    for (const addr of traders) {
        activityByTrader[addr] = await fetchTraderActivity(addr);
        process.stdout.write(".");
    }
    console.log("\nTrader activity fetched.");

    // Step 2: Map trades to conditionId/tokenId
    const positions: Record<string, PositionStats> = {};
    const uniqueConditionIds = new Set<string>();

    for (const t of previewTrades) {
        const activities = activityByTrader[t.traderAddress] || [];
        const match = activities.find((a: any) => 
            (a.title === t.market || a.market === t.market || a.slug === t.market) && 
            Math.abs(parseFloat(a.price) - t.price) < 0.05
        );

        if (!match) continue;

        const tokenId = match.asset || match.assetId || match.tokenId;
        const conditionId = match.conditionId;
        if (!tokenId || !conditionId) continue;

        if (!positions[tokenId]) {
            positions[tokenId] = {
                title: t.market,
                tokenId,
                conditionId,
                costBasis: 0,
                shares: 0,
                buys: 0,
                avgPrice: 0,
                currentPrice: 0,
                winner: false,
                resolved: false,
                pnl: 0,
                outcome: match.outcome || "Unknown"
            };
            uniqueConditionIds.add(conditionId);
        }

        const shares = t.copySize / t.price;
        positions[tokenId].costBasis += t.copySize;
        positions[tokenId].shares += shares;
        positions[tokenId].buys += 1;
    }

    console.log(`Resolved ${Object.keys(positions).length} unique outcomes in ${uniqueConditionIds.size} markets.`);
    console.log("Fetching market resolutions from CLOB...");

    // Step 3: Fetch market info from CLOB
    const marketCache: Record<string, MarketData> = {};
    for (const cid of uniqueConditionIds) {
        const data = await fetchClobMarket(cid);
        if (data) marketCache[cid] = data;
        process.stdout.write("#");
        await new Promise(r => setTimeout(r, 100));
    }
    console.log("\nMarket details fetched.");

    // Step 4: Calculate P&L
    let totalInvested = 0;
    let totalRealized = 0;
    let totalUnrealized = 0;
    let totalRedeemed = 0;

    for (const tokenId of Object.keys(positions)) {
        const p = positions[tokenId];
        const m = marketCache[p.conditionId];
        
        if (m) {
            const tokenInfo = m.tokens.find(t => t.tokenId === tokenId);
            if (tokenInfo) {
                p.currentPrice = tokenInfo.price;
                p.winner = tokenInfo.winner;
                p.outcome = tokenInfo.outcome;
                
                // If price is 1 or 0 and market is resolved (winner exists or price is exactly 0/1)
                // CLOB API doesn't explicitly have 'closed' but winner field is a strong indicator
                const isResolved = p.winner || (m.tokens.some(t => t.winner) || (tokenInfo.price === 0 || tokenInfo.price === 1));
                p.resolved = isResolved;

                p.pnl = (p.shares * p.currentPrice) - p.costBasis;
                
                if (isResolved) {
                    totalRealized += p.pnl;
                    if (p.winner) totalRedeemed += p.pnl;
                } else {
                    totalUnrealized += p.pnl;
                }
            }
        }
        totalInvested += p.costBasis;
    }

    console.log("\n--- BOT PERFORMANCE STATISTICS (PREVIEW MODE) ---");
    console.log(`Total Period:     ~15 hours`);
    console.log(`Total Trades:     ${previewTrades.length}`);
    console.log(`Total Invested:   $${totalInvested.toFixed(2)}`);
    console.log("--------------------------------------------------");
    console.log(`Realized P&L:     $${totalRealized.toFixed(2)}`);
    console.log(`Unrealized P&L:   $${totalUnrealized.toFixed(2)}`);
    console.log(`Net P&L:          $${(totalRealized + totalUnrealized).toFixed(2)}`);
    console.log("--------------------------------------------------");
    console.log(`PnL from Redemptions: $${totalRedeemed.toFixed(2)} (Winners)`);
    console.log("--------------------------------------------------\n");

    const results = Object.values(positions);
    const top = results.sort((a, b) => b.pnl - a.pnl).slice(0, 10);
    const bottom = results.sort((a, b) => a.pnl - b.pnl).slice(0, 10);

    console.log("Top 10 Performers:");
    top.forEach(r => console.log(`- ${r.title} (${r.outcome}): $${r.pnl.toFixed(2)} (${r.resolved ? "RESOLVED" : "OPEN"})`));

    console.log("\nBottom 10 Performers:");
    bottom.forEach(r => console.log(`- ${r.title} (${r.outcome}): $${r.pnl.toFixed(2)} (${r.resolved ? "RESOLVED" : "OPEN"})`));

    fs.writeFileSync("/tmp/pnl_results.json", JSON.stringify({
        summary: { totalInvested, totalRealized, totalUnrealized, totalRedeemed, netPnl: totalRealized + totalUnrealized },
        details: results
    }, null, 2));
}

run();
