import { describe, it, expect } from "vitest";
import { validatePrivateKey, validateAddress, parseAddresses } from "../config-validators";

describe("validatePrivateKey", () => {
  const validKey = "a".repeat(64);

  it("accepts valid 64-hex key", () => {
    expect(validatePrivateKey(validKey)).toBe(validKey);
  });

  it("strips 0x prefix", () => {
    expect(validatePrivateKey("0x" + validKey)).toBe(validKey);
  });

  it("throws on too short key", () => {
    expect(() => validatePrivateKey("abcd")).toThrow("64 hex");
  });

  it("throws on non-hex chars", () => {
    expect(() => validatePrivateKey("g".repeat(64))).toThrow("64 hex");
  });
});

describe("validateAddress", () => {
  const validAddr = "0x" + "a".repeat(40);

  it("accepts valid address", () => {
    expect(validateAddress(validAddr, "TEST")).toBe(validAddr);
  });

  it("throws without 0x prefix", () => {
    expect(() => validateAddress("a".repeat(40), "TEST")).toThrow("0x + 40 hex");
  });

  it("throws on wrong length", () => {
    expect(() => validateAddress("0x" + "a".repeat(20), "TEST")).toThrow("0x + 40 hex");
  });
});

describe("parseAddresses", () => {
  it("parses comma-separated addresses", () => {
    const result = parseAddresses("0xabc, 0xdef, 0x123");
    expect(result).toEqual(["0xabc", "0xdef", "0x123"]);
  });

  it("parses JSON array", () => {
    const result = parseAddresses('["0xabc","0xdef"]');
    expect(result).toEqual(["0xabc", "0xdef"]);
  });

  it("parses single address", () => {
    const result = parseAddresses("0xabc");
    expect(result).toEqual(["0xabc"]);
  });

  it("throws on non-string JSON array", () => {
    expect(() => parseAddresses("[123, null]")).toThrow("array of strings");
  });
});
