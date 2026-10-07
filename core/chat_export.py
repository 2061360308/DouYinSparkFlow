"""Collect visible text history without skipping virtual-list windows."""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

SHANGHAI = timezone(timedelta(hours=8))

# Locate the chat scroller from a message, never from the conversation sidebar.
JS_HISTORY_SCROLL = """(direction) => {
  const item = document.querySelector('[data-e2e="msg-item-content"]');
  if (!item) return null;
  let el = item.parentElement;
  while (el) {
    const css = getComputedStyle(el);
    if (/(auto|scroll)/.test(css.overflowY) && el.clientHeight > 0 &&
        el.scrollHeight > el.clientHeight + 1) break;
    el = el.parentElement;
  }
  if (!el) return null;
  const before = el.scrollTop;
  if (direction === 'bottom') el.scrollTo({top: el.scrollHeight, behavior: 'instant'});
  if (direction === 'older') {
    el.scrollTo({top: Math.max(0, before - Math.max(1, el.clientHeight * 0.55)), behavior: 'instant'});
    el.dispatchEvent(new Event('scroll', {bubbles: true}));
  }
  return {top: el.scrollTop, height: el.scrollHeight, viewport: el.clientHeight};
}"""


def collect_history(im, conv_id, *, stop, progress=lambda _: None,
                    timeout=900, max_steps=10000, clock=time.monotonic):
    """A stalled page is not evidence that server history is complete."""
    messages = {}
    start = clock()
    stalled = 0
    previous = None
    reason = "step_limit"
    try:
        im.page.evaluate(JS_HISTORY_SCROLL, 'bottom')
        im.page.wait_for_timeout(800)
        for _ in range(max_steps):
            if stop.is_set():
                reason = "cancelled"
                break
            if clock() - start >= timeout:
                reason = "timeout"
                break
            if str((im._current_conv() or {}).get('convId')) != str(conv_id):
                reason = "conversation_changed"
                break
            rows = im.read_chat_messages(conv_id)
            for row in rows:
                messages[str(row['id'])] = row
            position = im.page.evaluate(JS_HISTORY_SCROLL, 'probe')
            progress({'count': len(messages)})
            if position is None:
                # A short conversation might still be loading. Retry before stopping.
                signature = (len(messages), None)
            else:
                signature = (len(messages), position['top'], position['height'])
            stalled = stalled + 1 if signature == previous else 0
            previous = signature
            if stalled >= 12:
                reason = 'no_more_loaded' if position and position['top'] <= 1 else 'loading_stalled'
                break
            im.page.evaluate(JS_HISTORY_SCROLL, 'older')
            # Pump browser events; new older pages remain asynchronous.
            im.page.wait_for_timeout(700)
    except Exception:
        if not messages:
            raise
        reason = 'read_error'
    rows = sorted(messages.values(), key=lambda m: (m['created_at'],
                  int(m.get('order') or 0), str(m['id'])))
    return {'messages': rows, 'stop_reason': reason, 'complete': False,
            'elapsed_seconds': round(clock() - start, 1)}


REASONS = {
    'no_more_loaded': '页面顶部持续无新增，无法确认服务器是否还有历史',
    'loading_stalled': '页面加载或滚动停滞，可能还有更多历史',
    'timeout': '达到 15 分钟加载时限，可能还有更多历史',
    'step_limit': '达到滚动次数上限，可能还有更多历史',
    'cancelled': '用户停止，保留已读取的记录',
    'conversation_changed': '当前会话发生变化，停止读取',
    'read_error': '读取异常，保留已读取的记录',
}


def write_export(root, *, account, friend, conv_id, history):
    rows = history['messages']
    def label(timestamp):
        return datetime.fromtimestamp(timestamp, SHANGHAI).isoformat(timespec='seconds')
    meta = {k: v for k, v in history.items() if k != 'messages'}
    meta.update(account=account, friend=friend, conversation_id=str(conv_id),
                exported_at=datetime.now(SHANGHAI).isoformat(timespec='seconds'),
                timezone='Asia/Shanghai', message_count=len(rows),
                first_message_at=label(rows[0]['created_at']) if rows else None,
                last_message_at=label(rows[-1]['created_at']) if rows else None,
                scope='抖音网页版已加载的文字消息；不含图片、视频、语音、已撤回或已删除内容',
                stop_description=REASONS.get(history['stop_reason'], history['stop_reason']))
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', friend).strip(' .')[:60] or '好友'
    # A unique directory prevents overwrites and keeps paired files together.
    folder = Path(root) / (datetime.now(SHANGHAI).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    json_path = folder / ('聊天-' + safe + '.json')
    txt_path = folder / ('聊天-' + safe + '.txt')
    json_path.write_text(json.dumps({'metadata': meta, 'messages': rows}, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = [f'账号：{account}', f'好友：{friend}', f'会话 ID：{conv_id}',
             f'文字消息：{len(rows)} 条', f'范围：{meta["first_message_at"] or "无"} 至 {meta["last_message_at"] or "无"}',
             meta['scope'], '完整性：未确认全部历史', '停止原因：' + meta['stop_description'], '']
    for row in rows:
        sender = '我' if row['from_me'] else friend
        lines.extend([f'[{label(row["created_at"])}] {sender}', row['text'], ''])
    txt_path.write_text('\n'.join(lines), encoding='utf-8-sig')
    return {'folder': str(folder.resolve()), 'txt_path': str(txt_path.resolve()),
            'json_path': str(json_path.resolve()), 'metadata': meta}
