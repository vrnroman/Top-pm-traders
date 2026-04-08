import { createPublicClient, createWalletClient, http, parseGwei, zeroHash } from "viem";
import { polygon } from "viem/chains";
import { privateKeyToAccount } from "viem/accounts";
import type { Hex } from "viem";
import axios from "axios";
import { CONFIG } from "./config";
import { logger } from "./logger";
import { USDC_ADDRESS, CTF_CONTRACT, CTF_REDEEM_ABI } from "./constants";
import { recordSell, getPosition } from "./inventory";
import { errorMessage } from "./types";

interface RedeemablePosition {
  conditionId: string;
  tokenId: string;
  title: string;
  shares: number;
  curPrice: number; // ~1 = won, ~0 = lost
}

export interface RedeemDetail {
  title: string;
  shares: number;
  costBasis: number; // what we paid (shares × avgPrice)
  returned: number;  // what we got back (shares × $1 for wins)
}

export interface RedeemResult {
  count: number;
  markets: string[];
  totalShares: number;
  details: RedeemDetail[];
}

/** Fetch positions marked as redeemable by the Data API. */
async function fetchRedeemablePositions(): Promise<RedeemablePosition[]> {
  const res = await axios.get(`${CONFIG.dataApiUrl}/positions`, {
    params: { user: CONFIG.proxyWallet, redeemable: true },
    timeout: 15000,
  });

  if (!Array.isArray(res.data)) return [];

  const positions: RedeemablePosition[] = [];
  for (const p of res.data) {
    if (!p.conditionId || !p.asset) continue;
    // Skip neg-risk (multi-outcome) markets — they use NegRiskAdapter, not base CTF
    if (p.negRisk) continue;
    const size = typeof p.size === "string" ? parseFloat(p.size) : Number(p.size);
    if (!isFinite(size) || size <= 0) continue;
    const curPrice = typeof p.curPrice === "number" ? p.curPrice : parseFloat(p.curPrice || "0");
    positions.push({
      conditionId: p.conditionId,
      tokenId: p.asset,
      title: p.title || "Unknown market",
      shares: size,
      curPrice: isFinite(curPrice) ? curPrice : 0,
    });
  }
  return positions;
}

/** Check for redeemable positions and redeem them on-chain. */
export async function checkAndRedeemPositions(privateKey: string): Promise<RedeemResult> {
  const positions = await fetchRedeemablePositions();
  if (positions.length === 0) return { count: 0, markets: [], totalShares: 0, details: [] };

  logger.info(`Found ${positions.length} redeemable position(s)`);

  const account = privateKeyToAccount(`0x${privateKey}`);
  const publicClient = createPublicClient({ chain: polygon, transport: http(CONFIG.rpcUrl) });
  const walletClient = createWalletClient({ account, chain: polygon, transport: http(CONFIG.rpcUrl) });

  // Gas overrides — same pattern as check-approvals.ts
  const fees = await publicClient.estimateFeesPerGas();
  const baseFee = fees.maxFeePerGas ?? parseGwei("150");
  const gasOverrides = {
    maxFeePerGas: baseFee * 2n,
    maxPriorityFeePerGas: parseGwei("50"),
  };

  let count = 0;
  let totalShares = 0;
  const markets: string[] = [];
  const details: RedeemDetail[] = [];

  for (const pos of positions) {
    try {
      // Capture cost basis before redeem clears the position
      const invPos = getPosition(pos.tokenId);
      const costBasis = invPos ? invPos.shares * invPos.avgPrice : 0;

      const hash = await walletClient.writeContract({
        address: CTF_CONTRACT,
        abi: CTF_REDEEM_ABI,
        functionName: "redeemPositions",
        args: [USDC_ADDRESS, zeroHash, pos.conditionId as Hex, [1n, 2n]],
        ...gasOverrides,
      });
      await publicClient.waitForTransactionReceipt({ hash });
      logger.info(`Redeemed "${pos.title}" (${pos.shares} shares) — tx: ${hash}`);
      recordSell(pos.tokenId, pos.shares);
      count++;
      totalShares += pos.shares;
      markets.push(pos.title);
      // curPrice ~1 = won ($1/share returned), ~0 = lost ($0 returned)
      const returned = pos.curPrice > 0.5 ? pos.shares : 0;
      details.push({ title: pos.title, shares: pos.shares, costBasis, returned });
    } catch (err: unknown) {
      logger.warn(`Failed to redeem "${pos.title}": ${errorMessage(err)}`);
    }
  }

  return { count, markets, totalShares, details };
}
