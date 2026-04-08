import axios from "axios";
import { errorMessage } from "../types";

const DATA_API = "https://data-api.polymarket.com";
const CLOB_API = "https://clob.polymarket.com";

async function check(name: string, url: string): Promise<boolean> {
  try {
    const res = await axios.get(url, { timeout: 5000 });
    console.log(`  [OK] ${name} — status ${res.status}`);
    return true;
  } catch (err: unknown) {
    console.log(`  [FAIL] ${name} — ${errorMessage(err)}`);
    return false;
  }
}

async function main() {
  console.log("\n=== Polymarket Bot Health Check ===\n");

  const results = await Promise.all([
    check("Data API", `${DATA_API}/positions?user=0x0000000000000000000000000000000000000000`),
    check("CLOB API", `${CLOB_API}/ok`),
  ]);

  const allOk = results.every(Boolean);
  console.log(`\n${allOk ? "All checks passed." : "Some checks failed."}\n`);
  process.exit(allOk ? 0 : 1);
}

main();
