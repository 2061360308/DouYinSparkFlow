/**
 * core/msg_builder.js
 * 解析消息模板构建具体发送的消息内容
 * 移植自原项目 core/msg_builder.py
 */

import { getConfig } from "../utils/config.js";
import { requestHitokoto } from "../utils/hitokoto.js";

/**
 * 构建发送消息
 * 模板中的 [API] 占位符会被替换为一言内容
 */
export async function buildMessage() {
  let message = getConfig(globalThis.env).messageTemplate || "续火花";
  if (message.includes("[API]")) {
    const apiContent = await requestHitokoto();
    message = message.replace("[API]", apiContent);
  }
  return message.trim();
}
