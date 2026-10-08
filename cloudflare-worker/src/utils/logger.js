/**
 * utils/logger.js
 * 简单日志器，输出到 console（wrangler tail 可实时查看）
 * 移植自原项目 utils/logger.py
 */

const LEVELS = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 50, CRITICAL: 50 };

let currentLevel =
  LEVELS[(globalThis.LOG_LEVEL || "INFO").toUpperCase()] ?? LEVELS.INFO;

export function setLogLevel(level) {
  currentLevel = LEVELS[String(level).toUpperCase()] ?? LEVELS.INFO;
}

function ts() {
  return new Date().toISOString().replace("T", " ").replace("Z", "");
}

function fmt(args) {
  return args
    .map((a) => {
      if (typeof a === "string") return a;
      try {
        return JSON.stringify(a);
      } catch {
        return String(a);
      }
    })
    .join(" ");
}

export const logger = {
  debug: (...args) =>
    currentLevel <= LEVELS.DEBUG &&
    console.log(`[${ts()}] [DEBUG] ${fmt(args)}`),
  info: (...args) =>
    currentLevel <= LEVELS.INFO && console.log(`[${ts()}] [INFO] ${fmt(args)}`),
  warning: (...args) =>
    currentLevel <= LEVELS.WARNING &&
    console.warn(`[${ts()}] [WARNING] ${fmt(args)}`),
  error: (...args) =>
    currentLevel <= LEVELS.ERROR &&
    console.error(`[${ts()}] [ERROR] ${fmt(args)}`),
};
