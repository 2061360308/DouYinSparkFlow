<template>
  <div class="sidebar-content">
    <div class="sidebar-head">
      <ModeSwitcher :schedule="schedule" :proxy="proxy" @changed="$emit('changed', $event)" />
    </div>

    <nav class="nav-items">
      <button
        v-for="item in navItems"
        :key="item.id"
        class="nav-item"
        :class="{ active: activeView === item.id }"
        @click="$emit('navigate', item.id)"
      >
        <component :is="item.icon" class="nav-icon" :size="16" />
        <span class="nav-label">{{ item.label }}</span>
      </button>
    </nav>

    <div class="nav-group-label">资源</div>

    <nav class="nav-items">
      <div
        v-for="item in resourceItems"
        :key="item.id"
        class="nav-item res-item"
        :class="{ active: activeView === item.id }"
        @click="onResourceClick(item)"
      >
        <component :is="item.icon" class="nav-icon" :size="16" />
        <span class="nav-label">{{ item.label }}</span>
        <button
          type="button"
          class="res-action"
          title="在默认浏览器中打开"
          @click.stop="$emit('open-external', item.url)"
        >
          <ExternalLink :size="14" />
        </button>
      </div>
    </nav>

    <div class="sidebar-bottom">
      <button class="action-btn secondary" :disabled="loading" @click="$emit('reload')">重新载入</button>
    </div>
  </div>
</template>

<script setup>
import { ExternalLink } from '@lucide/vue'
import ModeSwitcher from './ModeSwitcher.vue'

defineProps({
  navItems: { type: Array, default: () => [] },
  resourceItems: { type: Array, default: () => [] },
  activeView: { type: String, default: '' },
  schedule: { type: Object, default: () => ({}) },
  proxy: { type: Object, default: () => ({}) },
  loading: { type: Boolean, default: false },
})
const emit = defineEmits(['navigate', 'changed', 'reload', 'open-external'])

// 能嵌入的（embed）切到内置 iframe；不能嵌入的（如 GitHub）直接用系统浏览器打开
function onResourceClick(item) {
  if (item.embed) emit('navigate', item.id)
  else emit('open-external', item.url)
}
</script>

<style scoped>
.sidebar-content {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--vg-bg-subtle);
  color: var(--vg-fg-2);
  user-select: none;
}

.sidebar-head { padding: 14px 12px 8px; }

.nav-items {
  padding: 6px 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.sidebar-content > .nav-items:first-of-type { flex: 1; }

.nav-item {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  text-align: left;
  padding: 7px 10px;
  border: none;
  border-radius: var(--vg-radius-sm);
  background: transparent;
  color: var(--vg-fg-2);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.14s ease, color 0.14s ease;
}

.nav-item:hover { background: var(--vg-bg-hover); color: var(--vg-fg); }

.nav-item.active {
  background: var(--vg-bg-active);
  color: var(--vg-fg);
  font-weight: 600;
}

.nav-icon { width: 16px; height: 16px; flex: 0 0 auto; opacity: 0.9; }
.nav-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 资源分组 */
.nav-group-label {
  padding: 16px 16px 4px;
  font-size: 11px;
  font-weight: 500;
  color: var(--vg-fg-4);
  letter-spacing: 0.06em;
}

.res-item { position: relative; }

.res-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  margin-left: 4px;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--vg-fg-3);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.12s ease, background 0.12s ease, color 0.12s ease;
}

.res-item:hover .res-action { opacity: 1; }
.res-action:hover { background: var(--vg-bg); color: var(--vg-fg); box-shadow: var(--vg-ring); }

.sidebar-bottom {
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
</style>
