/**
 * core/tasks.js
 * 续火核心逻辑，移植自原项目 core/tasks.py
 *
 * 与 Playwright 版本的差异：
 * - 运行时从 Playwright 换成 @cloudflare/puppeteer（Cloudflare Browser Rendering）
 * - Playwright 同步 API -> Puppeteer 异步 API
 * - page.on("response") 的好友信息收集改为 Promise + 事件监听
 * - Workers 单次唤醒最长约 10 分钟（keep_alive 上限），无浏览器内 sleep，用可中断的 sleep
 */

import puppeteer from "@cloudflare/puppeteer";
import { getConfig, getUserData } from "../utils/config.js";
import { logger } from "../utils/logger.js";
import { norm } from "../utils/norm.js";
import { buildMessage } from "./msg_builder.js";

const CONVERSATION_ITEM_SELECTOR = ".conversationConversationItemwrapper";
const CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle";
const CONVERSATION_LIST_SELECTOR = ".conversationConversationListwrapper";
const CHAT_EDITOR_SELECTOR = ".messageEditorimChatEditorContainer";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * 收集好友信息：监听 im/user/info 接口响应
 * 返回 { promise, cancel }，promise resolve 为 userIDDict
 */
function collectUserInfo(page) {
  const userIDDict = {};
  let resolveDone;
  let settled = false;

  const handler = async (response) => {
    if (!response.url().includes("aweme/v1/web/im/user/info")) return;
    try {
      const jsonData = await response.json();
      for (const item of jsonData?.data ?? []) {
        const nickname = norm(item.nickname);
        const remarkName = norm(item.remark_name ?? nickname);
        userIDDict[remarkName] = [
          item.short_id,
          item.unique_id,
          item.sec_uid ?? "",
          nickname,
          remarkName,
        ];
      }
    } catch (e) {
      logger.warning(`解析好友信息响应失败: ${e.message}`);
    }
  };

  page.on("response", handler);

  return {
    dict: userIDDict,
    cancel: () => {
      if (!settled) {
        settled = true;
        try {
          page.off?.("response", handler);
        } catch {
          /* 页面可能已销毁，忽略 */
        }
      }
    },
  };
}

/**
 * 通用重试逻辑，移植自 retry_operation()
 */
async function retryOperation(
  name,
  operation,
  retries = 3,
  delay = 2000,
  ...args
) {
  for (let attempt = 0; attempt < retries; attempt++) {
    try {
      return await operation(...args);
    } catch (e) {
      if (attempt < retries - 1) {
        logger.warning(
          `${name} 失败，正在重试第 ${attempt + 1} 次，错误：${e.message}`,
        );
        await sleep(delay);
      } else {
        logger.error(`${name} 失败，已达到最大重试次数，错误：${e.message}`);
        throw e;
      }
    }
  }
}

/**
 * 检查 targetName 是否为目标，移植自 checkTargetName()
 */
function checkTargetName(targetName, targets, userIDDict) {
  targetName = norm(targetName);
  if (userIDDict[targetName]) {
    const entry = userIDDict[targetName];
    return entry.find((v) => v && targets.includes(v)) ?? null;
  }
  return targets.includes(targetName) ? targetName : null;
}

/**
 * 滚动会话列表查找并选中目标好友，移植自 scroll_and_select_user()
 * 返回按顺序选中的目标列表
 */
async function scrollAndSelectUser(page, username, targets, userIDDict) {
  logger.debug(`账号 ${username} 开始查找目标好友列表`);
  logger.debug(`账号 ${username} 目标好友列表: ${targets.join(", ")}`);

  const foundTargets = new Set();
  const remainingTargets = new Set(targets);
  const selected = [];

  let emptyScrollCount = 0;
  const MAX_EMPTY_SCROLLS = 10;

  // 收集器提前创建，滚动过程中持续积累好友信息
  const collector = { dict: userIDDict };

  while (true) {
    const targetElements = await page.$$(CONVERSATION_ITEM_SELECTOR);
    const prevFoundCount = foundTargets.size;

    for (const element of targetElements) {
      try {
        const titleEl = await element.$(CONVERSATION_TITLE_SELECTOR);
        if (!titleEl) continue;
        const targetName = (
          await titleEl.evaluate((node) => node.textContent)
        )?.trim();
        if (!targetName || foundTargets.has(targetName)) continue;
        foundTargets.add(targetName);

        logger.debug(`账号 ${username} 找到好友 ${targetName}`);

        const targetSymbol = checkTargetName(
          targetName,
          targets,
          collector.dict,
        );
        if (targetSymbol) {
          await element.click();
          selected.push(targetSymbol);

          remainingTargets.delete(targetSymbol);
          if (remainingTargets.size === 0) {
            logger.debug(`账号 ${username} 所有目标好友均已找到，停止搜索`);
            return selected;
          }
          // 点击进入会话后需要返回列表才能继续选择，重新获取列表引用
          await returnToConversationList(page, username);
          break;
        }
      } catch (e) {
        logger.warning(`处理会话元素失败: ${e.message}`);
      }
    }

    // 本轮无新发现则累计空滚动计数
    if (foundTargets.size > prevFoundCount) {
      emptyScrollCount = 0;
    } else {
      emptyScrollCount += 1;
    }

    if (emptyScrollCount >= MAX_EMPTY_SCROLLS) {
      logger.warning(
        `账号 ${username} 连续 ${MAX_EMPTY_SCROLLS} 次滚动未发现新好友，判定已到达底部`,
      );
      if (remainingTargets.size > 0) {
        logger.warning(
          `账号 ${username} 搜索结束，仍有以下好友未找到: ${[...remainingTargets].join(", ")}`,
        );
      }
      return selected;
    }

    // 滚动会话列表容器
    const scrolled = await page.evaluate((sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const before = el.scrollTop;
      el.scrollTop += 800;
      return { before, after: el.scrollTop };
    }, CONVERSATION_LIST_SELECTOR);

    if (scrolled === null) {
      logger.error(`账号 ${username} 未找到滚动容器，退出`);
      return selected;
    }

    if (scrolled.before === scrolled.after) {
      emptyScrollCount += 2; // scrollTop 未变化，加速判定到底
      logger.debug(
        `账号 ${username} scrollTop 未变化 (${scrolled.before})，可能已到底 (空滚动计数: ${emptyScrollCount}/${MAX_EMPTY_SCROLLS})`,
      );
    } else {
      logger.debug(
        `账号 ${username} 滚动好友列表以加载更多好友 (scrollTop: ${scrolled.before} -> ${scrolled.after})`,
      );
    }

    await sleep(1500);
  }
}

/**
 * 从聊天界面返回会话列表
 * 原版 Playwright 中点击列表元素即可切换会话；Puppeteer 下点击会话项后
 * 页面停留在聊天界面，需要点返回按钮回到列表继续匹配下一个目标
 */
async function returnToConversationList(page, username) {
  // 优先点击"消息"侧栏入口 / 返回按钮（模糊匹配常见类名）
  const backSelectors = [
    'div[class*="imChatHeader"] div[class*="back"]',
    'div[class*="backBtn"]',
    'div[class*="im-header-back"]',
  ];
  for (const sel of backSelectors) {
    try {
      const btn = await page.waitForSelector(sel, { timeout: 2000 });
      if (btn) {
        await btn.click();
        await sleep(1000);
        return;
      }
    } catch {
      /* 尝试下一个选择器 */
    }
  }
  logger.debug(`账号 ${username} 未找到返回按钮，尝试直接继续列表操作`);
  await sleep(500);
}

/**
 * 执行单账号续火任务，移植自 do_user_task()
 */
async function doUserTask(browser, username, cookies, targets) {
  // 每个账号使用独立浏览器上下文，隔离 Cookie
  const context = await browser.createBrowserContext();
  const page = await context.newPage();

  // 收集好友信息监听器（在导航前挂载）
  const userInfo = collectUserInfo(page);

  // 注入 Cookie（Puppeteer 需要传入 URL 作用域）
  const cookieParams = cookies.map((c) => ({
    name: c.name,
    value: c.value,
    domain: c.domain ?? ".douyin.com",
    path: c.path ?? "/",
    secure: c.secure ?? true,
    httpOnly: c.httpOnly ?? false,
  }));
  await page.setCookie(...cookieParams);

  const config = getConfig(globalThis.env);

  // 打开抖音网页聊天页面
  await retryOperation(
    "打开抖音网页聊天页面",
    async (url) => {
      await page.goto(url, {
        waitUntil: "domcontentloaded",
        timeout: config.browserTimeout,
      });
    },
    config.taskRetryTimes,
    5000,
    "https://www.douyin.com/chat",
  );

  await sleep(5000); // 等待过可能存在的弹窗

  logger.debug(`账号 ${username} 开始发送消息`);

  try {
    const selectedTargets = await scrollAndSelectUser(
      page,
      username,
      targets,
      userInfo.dict,
    );

    if (selectedTargets.length === 0) {
      logger.warning(`账号 ${username} 未找到任何目标好友，本账号任务结束`);
      return;
    }

    const message = await buildMessage();

    // 从会话列表页面逐个进入会话发送
    for (const target of selectedTargets) {
      logger.debug(
        `账号 ${username} 准备发送消息给好友 ${target}：\n\t${message}`,
      );
      const sent = await sendMessageToTarget(
        page,
        username,
        target,
        targets,
        userInfo.dict,
        message,
      );
      if (sent) {
        logger.info(`账号 ${username} 给好友 ${target} 发送消息完成`);
      } else {
        logger.error(`账号 ${username} 给好友 ${target} 发送消息失败`);
      }
      await sleep(2000);
    }
  } finally {
    userInfo.cancel();
    await context.close().catch(() => {});
  }
}

/**
 * 进入指定好友的会话并发送消息
 * 点击会话列表中匹配的目标项，等待编辑器出现后输入并发送
 */
async function sendMessageToTarget(
  page,
  username,
  target,
  targets,
  userIDDict,
  message,
) {
  const config = getConfig(globalThis.env);

  // 在会话列表中找到目标并点击
  const clicked = await page.evaluate(
    (itemSel, titleSel, targetNames) => {
      const items = [...document.querySelectorAll(itemSel)];
      for (const item of items) {
        const title = item.querySelector(titleSel);
        if (!title) continue;
        if (targetNames.includes(title.textContent.trim())) {
          item.click();
          return title.textContent.trim();
        }
      }
      return null;
    },
    CONVERSATION_ITEM_SELECTOR,
    CONVERSATION_TITLE_SELECTOR,
    targets,
  );

  if (!clicked) {
    logger.warning(`账号 ${username} 未能重新定位好友会话，跳过 ${target}`);
    return false;
  }

  // 等待聊天输入框出现
  let editor;
  try {
    editor = await page.waitForSelector(CHAT_EDITOR_SELECTOR, {
      timeout: config.browserTimeout,
    });
  } catch {
    logger.error(`账号 ${username} 等待聊天输入框超时，跳过 ${target}`);
    return false;
  }

  // 逐行输入，行间用 Shift+Enter 换行
  const lines = message.split("\n");
  await editor.click();
  for (let i = 0; i < lines.length; i++) {
    await page.keyboard.type(lines[i], { delay: 30 });
    if (i < lines.length - 1) {
      await page.keyboard.down("Shift");
      await page.keyboard.press("Enter");
      await page.keyboard.up("Shift");
    }
  }

  // 回车发送
  await page.keyboard.press("Enter");
  await sleep(1000);
  return true;
}

/**
 * 主入口：遍历所有账号执行任务，移植自 runTasks()
 * @param {BrowserWorker} env.BROWSER - Cloudflare Browser Rendering 绑定
 */
export async function runTasks(env) {
  globalThis.env = env;
  const config = getConfig(env);
  const userData = getUserData(env);

  if (userData.length === 0) {
    logger.warning("没有可执行的任务，请检查 TASKS 与 COOKIES_* 配置");
    return { success: false, reason: "no-tasks" };
  }

  logger.info("开始执行任务");
  logger.debug(`消息模板: ${config.messageTemplate}`);
  logger.debug(`一言类型: ${config.hitokotoTypes.join(", ")}`);
  for (const user of userData) {
    logger.debug(
      `用户: ${user.username}, 目标好友: ${user.targets.join(", ")}`,
    );
  }

  // 连接 Browser Rendering，keep_alive 设为上限 10 分钟
  const browser = await puppeteer.launch(env.BROWSER, {
    keep_alive: 600000,
  });

  const results = [];
  try {
    for (const user of userData) {
      logger.info(`开始处理账号 ${user.username}`);
      try {
        await doUserTask(browser, user.username, user.cookies, user.targets);
        logger.info(`账号 ${user.username} 任务完成`);
        results.push({ username: user.username, ok: true });
      } catch (e) {
        logger.error(`账号 ${user.username} 任务失败: ${e.message}`);
        results.push({ username: user.username, ok: false, error: e.message });
      }
    }
  } finally {
    await browser.close().catch(() => {});
  }

  return { success: results.every((r) => r.ok), results };
}
