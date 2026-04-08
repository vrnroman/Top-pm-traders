import { parseAbi } from "viem";

// Polygon contract addresses used by Polymarket on the Polygon PoS chain.
// USDC.e is the bridged USDC that serves as Polymarket's collateral token.
export const USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174" as const;
export const CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E" as const;
export const NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a" as const;
export const CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045" as const;

// Shared ABI fragments — parsed for viem type safety
export const ERC20_BALANCE_ABI = parseAbi(["function balanceOf(address) view returns (uint256)"]);
export const ERC20_APPROVE_ABI = parseAbi([
  "function allowance(address owner, address spender) view returns (uint256)",
  "function approve(address spender, uint256 amount) returns (bool)",
]);
export const ERC1155_APPROVAL_ABI = parseAbi([
  "function isApprovedForAll(address account, address operator) view returns (bool)",
  "function setApprovalForAll(address operator, bool approved)",
]);

export const CTF_REDEEM_ABI = parseAbi([
  "function redeemPositions(address collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint256[] indexSets)",
]);

// Trading constants
export const FILL_CHECK_DELAY_MS = 3000;
export const FILL_CHECK_RETRIES = 2;
export const EXECUTION_LOOP_MS = 100;  // execution worker poll interval
