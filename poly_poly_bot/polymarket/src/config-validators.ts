/** Pure validation functions — no env vars, no side effects, safe to import in tests. */

export function parseAddresses(raw: string): string[] {
  const trimmed = raw.trim();
  if (trimmed.startsWith("[")) {
    const parsed = JSON.parse(trimmed);
    if (!Array.isArray(parsed) || !parsed.every((x) => typeof x === "string")) {
      throw new Error("USER_ADDRESSES JSON must be an array of strings");
    }
    return parsed;
  }
  return trimmed.split(",").map((a) => a.trim()).filter(Boolean);
}

export function validatePrivateKey(key: string): string {
  const clean = key.startsWith("0x") ? key.slice(2) : key;
  if (!/^[0-9a-fA-F]{64}$/.test(clean)) {
    throw new Error("PRIVATE_KEY must be 64 hex characters (without 0x prefix)");
  }
  return clean;
}

export function validateAddress(addr: string, name: string): string {
  if (!/^0x[0-9a-fA-F]{40}$/.test(addr)) {
    throw new Error(`${name} must be a valid Ethereum address (0x + 40 hex chars)`);
  }
  return addr;
}
