import { describe, it, expect } from "vitest";
import { sleep, shortAddress, roundCents } from "../utils";
import { errorMessage } from "../types";

describe("sleep", () => {
  it("resolves after approximately the given ms", async () => {
    const start = Date.now();
    await sleep(50);
    const elapsed = Date.now() - start;
    expect(elapsed).toBeGreaterThanOrEqual(40);
  });
});

describe("shortAddress", () => {
  it("abbreviates a full address", () => {
    expect(shortAddress("0x1234567890abcdef1234567890abcdef12345678")).toBe("0x1234...5678");
  });
});

describe("roundCents", () => {
  it("rounds to 2 decimal places", () => {
    expect(roundCents(1.006)).toBe(1.01);
    expect(roundCents(1.004)).toBe(1);
    expect(roundCents(99.999)).toBe(100);
    expect(roundCents(3.14159)).toBe(3.14);
  });
});

describe("errorMessage", () => {
  it("extracts message from Error", () => {
    expect(errorMessage(new Error("boom"))).toBe("boom");
  });

  it("converts string to string", () => {
    expect(errorMessage("oops")).toBe("oops");
  });

  it("converts number to string", () => {
    expect(errorMessage(42)).toBe("42");
  });
});
