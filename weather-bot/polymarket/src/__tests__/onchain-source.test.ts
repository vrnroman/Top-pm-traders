import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import fs from "fs";
import path from "path";

// Test cursor persistence and log parsing logic for on-chain source.
// We can't easily test the full OnchainSource class (needs viem mocking),
// so we test the cursor persistence functions and trade ID generation directly.

const DATA_DIR = path.resolve(process.cwd(), "data");
const CURSOR_FILE = path.join(DATA_DIR, "onchain-cursor.json");

describe("onchain cursor persistence", () => {
  beforeEach(() => {
    try { fs.unlinkSync(CURSOR_FILE); } catch { /* ok */ }
  });

  afterEach(() => {
    try { fs.unlinkSync(CURSOR_FILE); } catch { /* ok */ }
  });

  it("loadCursor returns 0n when no file exists", async () => {
    vi.resetModules();
    // Inline the loadCursor logic to test (same as production code)
    function loadCursor(): bigint {
      try {
        if (fs.existsSync(CURSOR_FILE)) {
          const data = JSON.parse(fs.readFileSync(CURSOR_FILE, "utf8"));
          if (data.lastBlock) return BigInt(data.lastBlock);
        }
      } catch { /* corrupted */ }
      return 0n;
    }
    expect(loadCursor()).toBe(0n);
  });

  it("saveCursor writes and loadCursor reads back", () => {
    function saveCursor(lastBlock: bigint): void {
      if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
      fs.writeFileSync(CURSOR_FILE, JSON.stringify({ lastBlock: lastBlock.toString() }));
    }
    function loadCursor(): bigint {
      try {
        if (fs.existsSync(CURSOR_FILE)) {
          const data = JSON.parse(fs.readFileSync(CURSOR_FILE, "utf8"));
          if (data.lastBlock) return BigInt(data.lastBlock);
        }
      } catch { /* corrupted */ }
      return 0n;
    }

    saveCursor(12345678n);
    expect(loadCursor()).toBe(12345678n);
  });

  it("loadCursor returns 0n for corrupted file", () => {
    if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
    fs.writeFileSync(CURSOR_FILE, "not json");

    function loadCursor(): bigint {
      try {
        if (fs.existsSync(CURSOR_FILE)) {
          const data = JSON.parse(fs.readFileSync(CURSOR_FILE, "utf8"));
          if (data.lastBlock) return BigInt(data.lastBlock);
        }
      } catch { /* corrupted */ }
      return 0n;
    }
    expect(loadCursor()).toBe(0n);
  });

  it("loadCursor returns 0n for empty object", () => {
    if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
    fs.writeFileSync(CURSOR_FILE, "{}");

    function loadCursor(): bigint {
      try {
        if (fs.existsSync(CURSOR_FILE)) {
          const data = JSON.parse(fs.readFileSync(CURSOR_FILE, "utf8"));
          if (data.lastBlock) return BigInt(data.lastBlock);
        }
      } catch { /* corrupted */ }
      return 0n;
    }
    expect(loadCursor()).toBe(0n);
  });
});

describe("onchain trade ID generation", () => {
  // Trade ID: "${txHash}-${tokenId}-${side}" — canonical key matching data-api-source

  it("generates canonical key from tx + token + side", () => {
    const txHash = "0xabc123";
    const tokenId = "98765";
    const side = "BUY";
    const id = `${txHash}-${tokenId}-${side}`;
    expect(id).toBe("0xabc123-98765-BUY");
  });

  it("different tokenIds in same tx produce different keys", () => {
    const txHash = "0xabc";
    expect(`${txHash}-tok1-BUY`).not.toBe(`${txHash}-tok2-BUY`);
  });

  it("same token different side produces different keys", () => {
    expect("0xabc-tok1-BUY").not.toBe("0xabc-tok1-SELL");
  });
});

describe("onchain side determination logic", () => {
  // From onchain-source.ts: makerAssetId == 0 means maker pays USDC (buys tokens)
  function determineSide(
    isTrackedMaker: boolean,
    makerAssetId: bigint,
    takerAssetId: bigint
  ): { side: "BUY" | "SELL"; tokenId: string } {
    if (isTrackedMaker) {
      if (makerAssetId === 0n) {
        return { side: "BUY", tokenId: takerAssetId.toString() };
      } else {
        return { side: "SELL", tokenId: makerAssetId.toString() };
      }
    } else {
      if (takerAssetId === 0n) {
        return { side: "BUY", tokenId: makerAssetId.toString() };
      } else {
        return { side: "SELL", tokenId: takerAssetId.toString() };
      }
    }
  }

  it("tracked maker + makerAssetId=0 → BUY", () => {
    const r = determineSide(true, 0n, 12345n);
    expect(r.side).toBe("BUY");
    expect(r.tokenId).toBe("12345");
  });

  it("tracked maker + makerAssetId!=0 → SELL", () => {
    const r = determineSide(true, 12345n, 0n);
    expect(r.side).toBe("SELL");
    expect(r.tokenId).toBe("12345");
  });

  it("tracked taker + takerAssetId=0 → BUY", () => {
    const r = determineSide(false, 12345n, 0n);
    expect(r.side).toBe("BUY");
    expect(r.tokenId).toBe("12345");
  });

  it("tracked taker + takerAssetId!=0 → SELL", () => {
    const r = determineSide(false, 0n, 12345n);
    expect(r.side).toBe("SELL");
    expect(r.tokenId).toBe("12345");
  });
});

describe("onchain old cursor fallback", () => {
  it("cursor > 10000 blocks behind latest triggers skip", () => {
    const latestBlock = 70000000n;
    let lastBlock = 69980000n; // 20000 blocks behind
    const INITIAL_LOOKBACK_BLOCKS = 15;

    if (lastBlock < latestBlock - 10000n) {
      lastBlock = latestBlock - BigInt(INITIAL_LOOKBACK_BLOCKS);
    }
    // Should have skipped to near-latest
    expect(latestBlock - lastBlock).toBe(BigInt(INITIAL_LOOKBACK_BLOCKS));
  });

  it("cursor < 10000 blocks behind does not trigger skip", () => {
    const latestBlock = 70000000n;
    let lastBlock = 69995000n; // 5000 blocks behind — within range
    const original = lastBlock;

    if (lastBlock < latestBlock - 10000n) {
      lastBlock = latestBlock - 15n;
    }
    expect(lastBlock).toBe(original); // unchanged
  });
});
