<template>
  <div class="mode-switcher">
    <el-dropdown
      trigger="click"
      placement="bottom-start"
      popper-class="mode-dropdown"
      @command="onCommand"
    >
      <button type="button" class="mode-trigger" :disabled="applying">
        <span class="mode-logo" v-html="icon(currentIcon)" />
        <span class="mode-text">
          <span class="mode-name">{{ currentLabel }}</span>
        </span>
        <span class="mode-caret" v-html="icon('caret')" />
      </button>

      <template #dropdown>
        <el-dropdown-menu>
          <div class="mode-menu-title">运行模式</div>
          <el-dropdown-item
            v-for="m in modes"
            :key="m.id"
            :command="m.id"
            :class="{ 'is-active': current === m.id }"
          >
            <span class="item-icon" v-html="icon(m.id)" />
            <span class="item-body">
              <span class="item-name">{{ m.label }}</span>
              <span class="item-desc">{{ m.desc }}</span>
            </span>
            <span v-if="current === m.id" class="item-check" v-html="icon('check')" />
          </el-dropdown-item>
        </el-dropdown-menu>
      </template>
    </el-dropdown>

    <TunnelModal
      :open="showTunnel"
      :proxy="tunnelProxy"
      :applying="applying"
      @confirm="onTunnelConfirm"
      @cancel="showTunnel = false"
    />
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { py } from '../api'
import TunnelModal from './TunnelModal.vue'

const props = defineProps({
  schedule: { type: Object, default: () => ({}) },
  proxy: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['changed'])

const applying = ref(false)
const showTunnel = ref(false)
const tunnelProxy = ref({ enabled: true })

const modes = [
  { id: 'scheduled', label: '常驻定时', desc: '本机每天定点执行' },
  { id: 'boot', label: '开机执行', desc: '本机开机后检测执行' },
  { id: 'config', label: '生成配置', desc: '只编辑配置供其他设备使用' },
]

const current = computed(() => props.schedule?.mode || '')
const currentIcon = computed(() => current.value || 'scheduled')
const currentLabel = computed(() => {
  const found = modes.find((m) => m.id === current.value)
  return found ? found.label : '未设置'
})

const ICONS = {
  scheduled: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  boot: '<path d="M12 3v9"/><path d="M6.5 6.5a8 8 0 1 0 11 0"/>',
  config:
    '<path d="M4 7h10"/><path d="M18 7h2"/><circle cx="16" cy="7" r="2"/><path d="M4 17h2"/><path d="M10 17h10"/><circle cx="8" cy="17" r="2"/>',
  caret: '<path d="M8 9l4-4 4 4"/><path d="M8 15l4 4 4-4"/>',
  check: '<path d="M20 6L9 17l-5-5"/>',
}

function icon(name) {
  const body = ICONS[name] || ''
  return (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    `stroke-linecap="round" stroke-linejoin="round">${body}</svg>`
  )
}

function onCommand(mode) {
  if (applying.value) return
  if (mode === 'config') {
    tunnelProxy.value = { ...(props.proxy || {}), enabled: true }
    showTunnel.value = true
    return
  }
  if (mode === current.value) return
  applyMode(mode)
}

async function applyMode(mode, proxy) {
  applying.value = true
  try {
    const res = await py('schedule_set_mode', proxy ? { mode, proxy } : { mode })
    const label = modes.find((m) => m.id === mode)?.label || mode
    ElMessage({ type: 'success', message: `已切换到「${label}」` })
    emit('changed', res.schedule)
    return true
  } catch (e) {
    ElMessage({ type: 'error', message: String(e.message || e) })
    return false
  } finally {
    applying.value = false
  }
}

async function onTunnelConfirm(draft) {
  const ok = await applyMode('config', draft)
  if (ok) showTunnel.value = false
}
</script>

<style scoped>
.mode-switcher { width: 100%; }

/* 让 el-dropdown 撑满，触发按钮才能真正 100% */
.mode-switcher :deep(.el-dropdown) { display: block; width: 100%; }

.mode-trigger {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 10px;
  border: none;
  border-radius: var(--vg-radius);
  background: transparent;
  color: inherit;
  cursor: pointer;
  text-align: left;
  transition: background 0.14s ease;
}

.mode-trigger:hover:not(:disabled) { background: var(--vg-bg-hover); }
.mode-trigger:disabled { opacity: 0.6; cursor: default; }

.mode-logo {
  width: 32px;
  height: 32px;
  border-radius: var(--vg-radius-sm);
  background: var(--vg-fg);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
}

.mode-logo :deep(svg) { width: 17px; height: 17px; }

.mode-text {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  line-height: 1.35;
}

.mode-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--vg-fg);
  letter-spacing: -0.01em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.mode-caret { color: var(--vg-fg-4); flex: 0 0 auto; }
.mode-caret :deep(svg) { width: 16px; height: 16px; }
</style>

<!-- 下拉菜单被 teleport 到 body，scoped 样式够不到，用非 scoped 覆盖 -->
<style>
.mode-dropdown {
  min-width: 240px !important;
  border-radius: var(--vg-radius) !important;
  box-shadow: var(--vg-shadow-pop) !important;
  border: none !important;
  padding: 4px !important;
}

.mode-dropdown .el-dropdown-menu { padding: 0; background: transparent; }

.mode-dropdown .mode-menu-title {
  font-size: 10.5px;
  color: var(--vg-fg-4);
  padding: 5px 8px 3px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.mode-dropdown .el-dropdown-menu__item {
  display: flex;
  align-items: center;
  gap: 10px;
  height: auto;
  padding: 8px;
  line-height: 1.35;
  border-radius: var(--vg-radius-sm);
}

.mode-dropdown .el-dropdown-menu__item:hover,
.mode-dropdown .el-dropdown-menu__item.is-active { background: var(--vg-bg-hover); }

.mode-dropdown .item-icon { width: 20px; height: 20px; color: var(--vg-fg-2); flex: 0 0 auto; }
.mode-dropdown .item-icon svg { width: 17px; height: 17px; }

.mode-dropdown .item-body { display: flex; flex-direction: column; flex: 1; min-width: 0; }
.mode-dropdown .item-name { font-size: 13px; color: var(--vg-fg); font-weight: 500; }
.mode-dropdown .item-desc { font-size: 11.5px; color: var(--vg-fg-4); }

.mode-dropdown .item-check { color: var(--vg-fg); flex: 0 0 auto; }
.mode-dropdown .item-check svg { width: 16px; height: 16px; }
</style>
