/**
 * utils/hitokoto.js
 * 一言 API 请求，移植自原项目 utils/hitokoto.py
 */

import { getConfig } from "./config.js";

const HITOKOTO_API = "https://v1.hitokoto.cn/";

const ALL_HITOKOTO_TYPES = {
  动画: "a",
  漫画: "b",
  游戏: "c",
  文学: "d",
  原创: "e",
  来自网络: "f",
  其他: "g",
  影视: "h",
  诗词: "i",
  哲学: "k",
  抖机灵: "l",
};

export async function requestHitokoto() {
  const config = getConfig(globalThis.env);

  let apiUrl = HITOKOTO_API;
  for (const [name, code] of Object.entries(ALL_HITOKOTO_TYPES)) {
    if (config.hitokotoTypes.includes(name)) {
      apiUrl += apiUrl.includes("?") ? `&c=${code}` : `?c=${code}`;
    }
  }

  try {
    const resp = await fetch(apiUrl, {
      signal: AbortSignal.timeout(10000),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const theFrom = data.from?.trim() || "未知来源";
    const theFromWho = data.from_who?.trim() || "未知作者";
    return `${data.hitokoto} —— ${theFrom} (${theFromWho})`;
  } catch (e) {
    console.error(`一言请求失败: ${e.message}`);
    return "[error] 无法获取一言内容";
  }
}
