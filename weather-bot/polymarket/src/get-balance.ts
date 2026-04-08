import { createPublicClient, http, formatUnits } from "viem";
import { polygon } from "viem/chains";
import { CONFIG } from "./config";
import { logger } from "./logger";
import { errorMessage } from "./types";
import { USDC_ADDRESS, ERC20_BALANCE_ABI } from "./constants";

const SPURIOUS_ZERO_THRESHOLD_USD = 10;
const ZERO_CONFIRM_COUNT = 3; // Accept $0 as real after N consecutive zero readings
let lastKnownBalance = -1;
let consecutiveZeros = 0;
const publicClient = createPublicClient({ chain: polygon, transport: http(CONFIG.rpcUrl) });

export async function getUsdcBalance(): Promise<number> {
  try {
    const raw = await publicClient.readContract({
      address: USDC_ADDRESS,
      abi: ERC20_BALANCE_ABI,
      functionName: "balanceOf",
      args: [CONFIG.proxyWallet as `0x${string}`],
    });
    const balance = parseFloat(formatUnits(raw as bigint, 6));

    if (balance === 0 && lastKnownBalance > SPURIOUS_ZERO_THRESHOLD_USD) {
      consecutiveZeros++;
      if (consecutiveZeros >= ZERO_CONFIRM_COUNT) {
        // Confirmed real zero — accept it
        logger.warn(`Balance confirmed $0 after ${consecutiveZeros} consecutive readings`);
        lastKnownBalance = 0;
        return 0;
      }
      logger.warn(`RPC returned $0 (${consecutiveZeros}/${ZERO_CONFIRM_COUNT}) — using cached $${lastKnownBalance.toFixed(2)}`);
      return lastKnownBalance;
    }

    consecutiveZeros = 0;
    lastKnownBalance = balance;
    return balance;
  } catch (err: unknown) {
    logger.error(`Failed to fetch USDC balance: ${errorMessage(err)}`);
    return lastKnownBalance > 0 ? lastKnownBalance : -1;
  }
}
