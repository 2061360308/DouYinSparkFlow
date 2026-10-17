/**
 * utils/config.js
 * 配置加载，移植自原项目 utils/config.py
 * Cloudflare Workers 无文件系统，配置全部来自环境变量（wrangler.toml [vars] 或 Secrets）
 */

import { logger, setLogLevel } from "./logger.js";
import { norm } from "./norm.js";

// 缓存绑定 env 引用：同一 isolate 内多次调用复用，换 env（测试/预览环境）时重新解析
let cachedEnv = null;
let cachedConfig = null;
let cachedUserData = null;

/**
 * 获取配置信息
 */
export function getConfig(env) {
  if (cachedConfig && cachedEnv === env) return cachedConfig;

  cachedEnv = env;
  cachedConfig = {
    messageTemplate:
      env.MESSAGE_TEMPLATE ??
      "[盖瑞]今日火花[加一]\n—— [右边] 每日一言 [左边] ——\n[API]",
    hitokotoTypes: parseJsonArray(env.HITOKOTO_TYPES, [
      "文学",
      "影视",
      "诗词",
      "哲学",
    ]),
    browserTimeout: intOrDefault(env.BROWSER_TIMEOUT, 60000), // Worker 会话最长 10 分钟，默认超时收紧到 60s
    friendListTimeout: intOrDefault(env.FRIEND_LIST_WAIT_TIME, 2000),
    taskRetryTimes: intOrDefault(env.TASK_RETRY_TIMES, 3),
    logLevel: env.LOG_LEVEL ?? "INFO",
  };

  setLogLevel(cachedConfig.logLevel);
  return cachedConfig;
}

/**
 * 获取任务列表
 * 环境变量格式与原项目一致：
 * - TASKS: JSON 数组 [{"username":"账号1","unique_id":"12345678905","targets":["好友A"]}]
 * - COOKIES_<UNIQUE_ID大写>: 浏览器导出的 Cookie JSON 数组
 */
export function getUserData(env) {
  if (cachedUserData && cachedEnv === env) return cachedUserData;

  cachedEnv = env;
  cachedUserData = [];
  const tasks = parseJsonArray(env.TASKS, []);

  for (const task of tasks) {
    const username = task.username ?? "未知用户";
    const uniqueId = task.unique_id;
    if (!uniqueId) {
      logger.warning(`${username} 的任务缺少 unique_id 字段，已跳过`);
      continue;
    }
    const cookiesKey = `COOKIES_${uniqueId}`.toUpperCase();
    const cookiesStr = env[cookiesKey] ?? "";
    if (!cookiesStr) {
      logger.warning(`${username} 的任务缺少 ${cookiesKey} 环境变量，已跳过`);
      continue;
    }
    let cookies;
    try {
      cookies = JSON.parse(cookiesStr);
    } catch {
      logger.warning(`${username} 的任务 ${cookiesKey} 格式不正确，已跳过`);
      continue;
    }
    if (!Array.isArray(cookies) || cookies.length === 0) {
      logger.warning(
        `${username} 的任务 ${cookiesKey} 不是非空 JSON 数组，已跳过`,
      );
      continue;
    }

    cachedUserData.push({
      uniqueId,
      username,
      cookies,
      targets: (task.targets ?? []).map((t) => norm(t)),
    });
  }

  return cachedUserData;
}

function parseJsonArray(str, fallback) {
  if (!str) return fallback;
  try {
    const v = JSON.parse(str);
    return Array.isArray(v) ? v : fallback;
  } catch {
    return fallback;
  }
}

function intOrDefault(str, dft) {
  const n = parseInt(str, 10);
  return Number.isFinite(n) ? n : dft;
}
