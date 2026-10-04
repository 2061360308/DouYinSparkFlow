// 与 Python 侧 app/web/bridge.py 配套的薄封装。
// 页面 -> Python：window.$py(method, payload)，返回值已是 JSON 安全类型；
// Python -> 页面：window.__pyOn(event, data)，由宿主 page.evaluate 推送。
//
// 注意：宿主先加载页面、再暴露桥（暴露在导航前的桥会变成坏的函数），
// 所以页面在桥就绪之前发起的调用要排队，等就绪后统一放行。

const handlers = new Map()
const pending = []

// 注册在 window 上：主循环用 page.evaluate 调用它推事件
window.__pyOn = (event, data) => {
  const handler = handlers.get(event)
  if (handler) handler(data)
}

function callNow(method, payload) {
  return window.$py(method, payload ?? null).then((res) => {
    if (res && typeof res === 'object' && 'error' in res) {
      const err = new Error(String(res.error))
      err.pyError = true
      throw err
    }
    return res
  })
}

export function py(method, payload) {
  if (typeof window.$py === 'function') return callNow(method, payload)
  return new Promise((resolve, reject) => {
    pending.push({ method, payload, resolve, reject })
  })
}

function flushPending() {
  if (typeof window.$py !== 'function') return
  if (!pending.length) return
  const snapshot = pending.splice(0)
  for (const item of snapshot) {
    callNow(item.method, item.payload).then(item.resolve, item.reject)
  }
}

setInterval(flushPending, 120)

export function on(event, handler) {
  handlers.set(event, handler)
  return () => handlers.delete(event)
}