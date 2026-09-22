/**
 * DouYinSparkFlow Cloudflare Workers 版本入口
 *
 * 功能：
 * 1. Cron Triggers 定时触发续火任务（默认每天 09:00 北京时间 = 01:00 UTC）
 * 2. HTTP 手动触发：GET /run（建议配置 RUN_TOKEN 后用 ?token= 或 X-Run-Token 头鉴权）
 * 3. GET /          返回服务状态
 */

import { runTasks } from "./core/tasks.js";
import { getConfig, getUserData } from "./utils/config.js";
import { logger } from "./utils/logger.js";

export default {
  /**
   * Cron Triggers 定时入口
   */
  async scheduled(controller, env, ctx) {
    globalThis.env = env;
    logger.info(`定时任务触发 (cron: ${controller.cron})`);
    try {
      const result = await runTasks(env);
      logger.info(`任务执行结束: ${JSON.stringify(result)}`);
    } catch (e) {
      logger.error(`定时任务执行失败: ${e.message}\n${e.stack}`);
    }
  },

  /**
   * HTTP 入口：手动触发 / 状态查询
   */
  async fetch(request, env, ctx) {
    globalThis.env = env;
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === "/" || path === "/status") {
      const config = getConfig(env);
      const userData = getUserData(env);
      return jsonResponse({
        service: "DouYinSparkFlow Worker",
        status: "ok",
        accounts: userData.length,
        accountsDetail: userData.map((u) => ({
          username: u.username,
          uniqueId: u.uniqueId,
          targets: u.targets,
        })),
        messageTemplate: config.messageTemplate,
        hitokotoTypes: config.hitokotoTypes,
        logLevel: config.logLevel,
      });
    }

    if (path === "/run") {
      // 鉴权：配置了 RUN_TOKEN 时校验 query token 或请求头
      if (env.RUN_TOKEN) {
        const token =
          url.searchParams.get("token") ?? request.headers.get("X-Run-Token");
        if (token !== env.RUN_TOKEN) {
          return jsonResponse({ error: "unauthorized" }, 401);
        }
      }

      logger.info("手动触发任务");
      // 前台等待执行完成（浏览器任务耗时 1-5 分钟，HTTP 请求超时上限内通常可完成）
      try {
        const result = await runTasks(env);
        return jsonResponse({ triggered: true, ...result });
      } catch (e) {
        logger.error(`手动任务执行失败: ${e.message}\n${e.stack}`);
        return jsonResponse({ triggered: true, error: e.message }, 500);
      }
    }

    return jsonResponse(
      { error: "not found", paths: ["/", "/status", "/run"] },
      404,
    );
  },
};

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}
