import { createPublicClient, createWalletClient, http, maxUint256, parseUnits, parseGwei } from "viem";
import { polygon } from "viem/chains";
import { privateKeyToAccount } from "viem/accounts";
import { CONFIG } from "./config";
import { logger } from "./logger";
import { USDC_ADDRESS, CTF_EXCHANGE, NEG_RISK_CTF_EXCHANGE, CTF_CONTRACT, ERC20_APPROVE_ABI, ERC1155_APPROVAL_ABI } from "./constants";

export async function checkAndSetApprovals(privateKey: string): Promise<void> {
  const account = privateKeyToAccount(`0x${privateKey}`);
  const publicClient = createPublicClient({ chain: polygon, transport: http(CONFIG.rpcUrl) });
  const walletClient = createWalletClient({ account, chain: polygon, transport: http(CONFIG.rpcUrl) });
  // Use PROXY_WALLET for checking approvals (may differ from EOA for Gnosis Safe)
  const address = CONFIG.proxyWallet as `0x${string}`;

  // Check USDC allowance for Exchange
  const allowance = await publicClient.readContract({
    address: USDC_ADDRESS,
    abi: ERC20_APPROVE_ABI,
    functionName: "allowance",
    args: [address, CTF_EXCHANGE],
  });

  // Polygon requires higher gas prices — fetch current and add buffer
  const fees = await publicClient.estimateFeesPerGas();
  const baseFee = fees.maxFeePerGas ?? parseGwei("150");
  const gasOverrides = {
    maxFeePerGas: baseFee * 2n,
    maxPriorityFeePerGas: parseGwei("50"),
  };

  if ((allowance as bigint) < parseUnits("1000000", 6)) {
    logger.info("Setting USDC approval for Polymarket Exchange...");
    const hash = await walletClient.writeContract({
      address: USDC_ADDRESS,
      abi: ERC20_APPROVE_ABI,
      functionName: "approve",
      args: [CTF_EXCHANGE, maxUint256],
      ...gasOverrides,
    });
    await publicClient.waitForTransactionReceipt({ hash });
    logger.info(`USDC approved. TX: ${hash}`);
  } else {
    logger.info("USDC approval: OK");
  }

  // Check ERC1155 (Conditional Tokens) approval for Exchange
  const isApproved = await publicClient.readContract({
    address: CTF_CONTRACT,
    abi: ERC1155_APPROVAL_ABI,
    functionName: "isApprovedForAll",
    args: [address, CTF_EXCHANGE],
  });

  if (!isApproved) {
    logger.info("Setting Conditional Tokens approval for Polymarket Exchange...");
    const hash = await walletClient.writeContract({
      address: CTF_CONTRACT,
      abi: ERC1155_APPROVAL_ABI,
      functionName: "setApprovalForAll",
      args: [CTF_EXCHANGE, true],
      ...gasOverrides,
    });
    await publicClient.waitForTransactionReceipt({ hash });
    logger.info(`Conditional Tokens approved. TX: ${hash}`);
  } else {
    logger.info("Conditional Tokens approval: OK");
  }

  // Neg Risk CTF Exchange — required for negatively-correlated markets (sports spreads, etc.)
  const negAllowance = await publicClient.readContract({
    address: USDC_ADDRESS,
    abi: ERC20_APPROVE_ABI,
    functionName: "allowance",
    args: [address, NEG_RISK_CTF_EXCHANGE],
  });
  if ((negAllowance as bigint) < parseUnits("1000000", 6)) {
    logger.info("Setting USDC approval for Neg Risk Exchange...");
    const hash = await walletClient.writeContract({
      address: USDC_ADDRESS,
      abi: ERC20_APPROVE_ABI,
      functionName: "approve",
      args: [NEG_RISK_CTF_EXCHANGE, maxUint256],
      ...gasOverrides,
    });
    await publicClient.waitForTransactionReceipt({ hash });
    logger.info(`Neg Risk USDC approved. TX: ${hash}`);
  } else {
    logger.info("Neg Risk USDC approval: OK");
  }

  const negCtfApproved = await publicClient.readContract({
    address: CTF_CONTRACT,
    abi: ERC1155_APPROVAL_ABI,
    functionName: "isApprovedForAll",
    args: [address, NEG_RISK_CTF_EXCHANGE],
  });
  if (!negCtfApproved) {
    logger.info("Setting Conditional Tokens approval for Neg Risk Exchange...");
    const hash = await walletClient.writeContract({
      address: CTF_CONTRACT,
      abi: ERC1155_APPROVAL_ABI,
      functionName: "setApprovalForAll",
      args: [NEG_RISK_CTF_EXCHANGE, true],
      ...gasOverrides,
    });
    await publicClient.waitForTransactionReceipt({ hash });
    logger.info(`Neg Risk CTF approved. TX: ${hash}`);
  } else {
    logger.info("Neg Risk Conditional Tokens approval: OK");
  }
}
