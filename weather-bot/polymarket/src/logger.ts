import fs, { createWriteStream, WriteStream } from "fs";
import path from "path";

const COLORS = {
  reset: "\x1b[0m",
  gray: "\x1b[90m",
  green: "\x1b[32m",
  yellow: "\x1b[33m",
  red: "\x1b[31m",
  cyan: "\x1b[36m",
  magenta: "\x1b[35m",
} as const;

const LEVELS = {
  debug: { color: COLORS.gray, label: "DEBUG" },
  info: { color: COLORS.green, label: "INFO " },
  warn: { color: COLORS.yellow, label: "WARN " },
  error: { color: COLORS.red, label: "ERROR" },
  trade: { color: COLORS.cyan, label: "TRADE" },
  skip: { color: COLORS.magenta, label: "SKIP " },
} as const;

type Level = keyof typeof LEVELS;

const LOG_FORMAT = process.env.LOG_FORMAT || "text"; // "text" (human) or "json" (ops)
const logsDir = path.resolve(process.cwd(), "logs");
if (!fs.existsSync(logsDir)) fs.mkdirSync(logsDir, { recursive: true });

let logStream: WriteStream | null = null;
let currentLogDate = "";

function getLogStream(): WriteStream {
  const date = new Date().toISOString().slice(0, 10);
  if (date !== currentLogDate || !logStream) {
    logStream?.end();
    currentLogDate = date;
    const logFile = path.join(logsDir, `bot-${date}.log`);
    logStream = createWriteStream(logFile, { flags: "a" });
  }
  return logStream;
}

function timestamp(): string {
  return new Date().toISOString().replace("T", " ").slice(0, 19);
}

function stripAnsi(str: string): string {
  // eslint-disable-next-line no-control-regex
  return str.replace(/\x1b\[[0-9;]*m/g, "");
}

function log(level: Level, msg: string, ...args: unknown[]): void {
  const { color, label } = LEVELS[level];
  const ts = timestamp();

  if (LOG_FORMAT === "json") {
    // Structured JSON for ops/monitoring tools
    const entry = JSON.stringify({ ts: new Date().toISOString(), level: label.trim().toLowerCase(), msg, ...(args.length > 0 ? { data: args } : {}) });
    if (level === "error") {
      console.error(entry);
    } else {
      console.log(entry);
    }
    getLogStream().write(entry + "\n");
  } else {
    // Human-readable with colors
    const formatted = `${COLORS.gray}${ts}${COLORS.reset} ${color}${label}${COLORS.reset} ${msg}`;
    if (level === "error") {
      console.error(formatted, ...args);
    } else {
      console.log(formatted, ...args);
    }
    const plain = `${ts} ${label} ${stripAnsi(msg)}\n`;
    getLogStream().write(plain);
  }
}

export const logger = {
  debug: (msg: string, ...args: unknown[]) => log("debug", msg, ...args),
  info: (msg: string, ...args: unknown[]) => log("info", msg, ...args),
  warn: (msg: string, ...args: unknown[]) => log("warn", msg, ...args),
  error: (msg: string, ...args: unknown[]) => log("error", msg, ...args),
  trade: (msg: string, ...args: unknown[]) => log("trade", msg, ...args),
  skip: (msg: string, ...args: unknown[]) => log("skip", msg, ...args),
  /** Flush and close log stream — call before process.exit(). */
  flush: (): Promise<void> => {
    return new Promise((resolve) => {
      if (logStream) {
        logStream.end(() => { logStream = null; resolve(); });
      } else {
        resolve();
      }
    });
  },
};
