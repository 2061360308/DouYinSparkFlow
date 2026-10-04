<template>
  <div v-if="open" class="modal-mask" @click.self="$emit('cancel')">
    <div class="modal-panel">
      <div class="modal-head">生成配置 · 配置隧道</div>
      <p class="modal-hint">
        本机只生成配置、拿到别的服务器（Docker）上跑，需要隧道让抓 Cookie 的出口 IP 与任务一致。
      </p>
      <TunnelForm :proxy="draft" @change="() => {}" />
      <div class="modal-actions">
        <button class="btn" @click="$emit('cancel')">取消</button>
        <button class="btn primary" :disabled="applying" @click="onConfirm">保存并切换</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import TunnelForm from './TunnelForm.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  proxy: { type: Object, default: () => ({}) },
  applying: { type: Boolean, default: false },
})
const emit = defineEmits(['confirm', 'cancel'])

const draft = ref({ enabled: true })

watch(
  () => [props.open, props.proxy],
  () => {
    if (props.open) {
      draft.value = { ...(props.proxy || {}), enabled: true }
    }
  },
  { immediate: true, deep: true },
)

function onConfirm() {
  const value = draft.value || {}
  if (!value.enabled) {
    ElMessage({ type: 'warning', message: '「生成配置」模式需要启用隧道' })
    return
  }
  if (!String(value.tunnel || '').startsWith('ws')) {
    ElMessage({ type: 'warning', message: '隧道地址要以 ws:// 或 wss:// 开头' })
    return
  }
  emit('confirm', { ...value })
}
</script>

<style scoped>
.modal-mask {
  position: fixed; inset: 0; z-index: 200;
  background: rgba(0, 0, 0, 0.32);
  backdrop-filter: blur(2px);
  display: flex; align-items: center; justify-content: center;
}

.modal-panel {
  width: 520px; max-width: 92vw;
  background: var(--vg-bg); border-radius: var(--vg-radius-lg);
  box-shadow: var(--vg-shadow-pop);
  padding: 20px 22px;
}

.modal-head { font-size: 15px; font-weight: 600; color: var(--vg-fg); letter-spacing: -0.01em; margin-bottom: 8px; }
.modal-hint { font-size: 12.5px; color: var(--vg-fg-2); line-height: 1.7; margin-bottom: 16px; }

.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }

.btn {
  padding: 7px 14px; border: none; border-radius: var(--vg-radius-sm);
  background: var(--vg-bg); color: var(--vg-fg); font-size: 13px; font-weight: 500;
  cursor: pointer; box-shadow: var(--vg-ring); transition: background 0.14s ease;
}

.btn:hover:not(:disabled) { background: var(--vg-bg-hover); }
.btn:disabled { opacity: 0.45; cursor: default; }

.btn.primary { background: var(--vg-fg); color: #fff; box-shadow: none; }
.btn.primary:hover:not(:disabled) { background: #000; }
</style>
