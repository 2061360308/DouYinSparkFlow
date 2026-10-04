<template>
  <div class="session-mask" @click.self="$emit('close')">
    <div class="session-panel">
      <div class="sp-head">
        <span class="sp-title">{{ session.title }}</span>
        <span v-if="session.uniqueId" class="sp-sub">{{ session.uniqueId }}</span>
      </div>

      <div class="sp-status" :class="{ ok: done, err: hasError }">
        <span class="sp-dot" :class="{ ok: done, err: hasError }" />
        {{ statusText }}
      </div>

      <div v-if="names.length" class="sp-names">已读到 {{ names.length }} 个会话，正在保存…</div>

      <div v-if="detected && (detected.nickname || detected.unique_id)" class="sp-detected">
        识别到：<b>{{ detected.nickname || '（无昵称）' }}</b>
        <span class="dim">（{{ detected.unique_id || '未读到抖音号' }}）</span>
      </div>

      <div class="sp-log">
        <div v-for="(line, i) in logs" :key="i" class="sp-line" :class="{ err: line.err }">
          <span class="sp-time">{{ line.time }}</span>{{ line.text }}
        </div>
      </div>

      <div class="sp-actions">
        <button class="btn" :disabled="done" @click="reopen">重新打开浏览器</button>
        <button v-if="session.kind === 'login'" class="btn primary" :disabled="done" @click="grabNow">
          立即抓取
        </button>
        <button class="btn danger" @click="closeSession">{{ done ? '关闭' : '取消' }}</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { py, on } from '../api'

const props = defineProps({
  session: { type: Object, required: true },
})

const emit = defineEmits(['saved-login', 'conversations-saved', 'close'])

const logs = ref([])
const statusText = ref('正在启动…')
const hasError = ref(false)
const done = ref(false)
const detected = ref(null)
const names = ref([])

let unsubscribe = null

function log(text, isError = false) {
  const now = new Date()
  const time = [now.getHours(), now.getMinutes(), now.getSeconds()]
    .map((n) => String(n).padStart(2, '0'))
    .join(':')
  logs.value.push({ time, text, err: isError })
  if (isError) hasError.value = true
}

function handleEvent(data) {
  if (!data || data.session !== props.session.id) return
  const kind = data.kind
  const payload = data.data || {}
  switch (kind) {
    case 'log':
      log(String(payload))
      break
    case 'status':
      statusText.value = String(payload || '')
      break
    case 'probe':
      if (payload.running === false) {
        if (!done.value) statusText.value = '浏览器已关闭'
      } else if (payload.detected && (payload.detected.nickname || payload.detected.unique_id)) {
        detected.value = payload.detected
      }
      break
    case 'conversation_progress':
      statusText.value = `正在滚动加载会话… 已读到 ${payload.count ?? 0} 个`
      break
    case 'conversations':
      names.value = payload.names || []
      break
    case 'error':
      log(String(payload), true)
      statusText.value = String(payload)
      break
    case 'saved':
      done.value = true
      if (payload.kind === 'login') {
        statusText.value = '已保存登录信息，正在关闭浏览器…'
        emit('saved-login', payload)
      } else {
        statusText.value = `已保存 ${payload.names?.length ?? 0} 个会话，正在关闭浏览器…`
        emit('conversations-saved', payload)
      }
      break
    case 'closed':
      emit('close')
      break
  }
}

async function grabNow() {
  try {
    const res = await py('account_grab', { session: props.session.id, manual: true })
    if (res?.ok) log('正在手动抓取登录信息…')
  } catch (e) {
    log(String(e.message || e), true)
  }
}

async function reopen() {
  try {
    const res = await py('account_open_browser', { session: props.session.id })
    if (res?.ok) log('正在重新打开浏览器…')
  } catch (e) {
    log(String(e.message || e), true)
  }
}

async function closeSession() {
  try {
    await py('account_shutdown', { session: props.session.id })
  } catch (e) {
    log(String(e.message || e), true)
  }
  emit('close')
}

onMounted(() => {
  unsubscribe = on('browser_event', handleEvent)
  log('会话已建立，等待浏览器就绪…')
})

onUnmounted(() => {
  if (unsubscribe) unsubscribe()
})
</script>

<style scoped>
.session-mask {
  position: fixed;
  inset: 0;
  z-index: 200;
  background: rgba(0, 0, 0, 0.32);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
}

.session-panel {
  width: 560px;
  max-width: 92vw;
  background: var(--vg-bg);
  border-radius: var(--vg-radius-lg);
  box-shadow: var(--vg-shadow-pop);
  padding: 20px 22px;
}

.sp-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 12px;
}

.sp-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--vg-fg);
  letter-spacing: -0.01em;
}

.sp-sub {
  font-size: 12px;
  color: var(--vg-fg-3);
  font-family: var(--vg-mono);
}

.sp-status {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--vg-fg-2);
  background: var(--vg-bg-subtle);
  border-radius: var(--vg-radius-sm);
  box-shadow: var(--vg-ring-soft);
  padding: 8px 12px;
  margin-bottom: 10px;
}

.sp-status.ok {
  background: #ecfdf5;
  box-shadow: 0 0 0 1px #a7f3d0;
  color: #065f46;
}

.sp-status.err {
  background: #fef2f2;
  box-shadow: 0 0 0 1px #fecaca;
  color: #991b1b;
}

.sp-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--vg-fg);
  flex-shrink: 0;
}

.sp-dot.ok { background: #10b981; }
.sp-dot.err { background: #ef4444; }

.sp-names {
  font-size: 12px;
  color: #0f766e;
  margin-bottom: 10px;
}

.sp-detected {
  font-size: 12.5px;
  color: var(--vg-fg);
  background: var(--vg-bg-subtle);
  border-radius: var(--vg-radius-sm);
  box-shadow: var(--vg-ring-soft);
  padding: 8px 12px;
  margin-bottom: 10px;
}

.sp-detected .dim { color: var(--vg-fg-3); }

.sp-log {
  height: 240px;
  overflow-y: auto;
  background: #0a0a0a;
  border-radius: var(--vg-radius);
  padding: 10px 12px;
  margin-bottom: 14px;
  font-family: var(--vg-mono);
  font-size: 11.5px;
  line-height: 1.65;
}

.sp-line { color: #d4d4d4; word-break: break-all; }
.sp-line.err { color: #ff8f8f; }

.sp-time { color: #6b6b6b; margin-right: 8px; user-select: none; }

.sp-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}

.btn {
  padding: 7px 14px;
  border: none;
  border-radius: var(--vg-radius-sm);
  background: var(--vg-bg);
  color: var(--vg-fg);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  box-shadow: var(--vg-ring);
  transition: background 0.14s ease;
}

.btn:hover:not(:disabled) { background: var(--vg-bg-hover); }
.btn:disabled { opacity: 0.45; cursor: default; }

.btn.primary {
  background: var(--vg-fg);
  color: #fff;
  box-shadow: none;
}

.btn.primary:hover:not(:disabled) { background: #000; }

.btn.danger {
  background: var(--vg-bg);
  color: var(--vg-danger);
  box-shadow: 0 0 0 1px rgba(217, 48, 37, 0.25);
}

.btn.danger:hover { background: var(--vg-danger); color: #fff; }
</style>