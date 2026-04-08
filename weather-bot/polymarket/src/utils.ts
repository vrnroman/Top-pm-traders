export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function shortAddress(addr: string): string {
  return `${addr.slice(0, 6)}...${addr.slice(-4)}`;
}

export function roundCents(n: number): number {
  return Math.round(n * 100) / 100;
}

export function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}
