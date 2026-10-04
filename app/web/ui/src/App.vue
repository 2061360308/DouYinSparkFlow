<template>
  <el-container class="app">
    <el-aside v-if="showAside" width="200px" class="app-aside">
      <SidebarContent
        :nav-items="visibleNavItems"
        :resource-items="resourceItems"
        :active-view="activeView"
        :schedule="state?.schedule"
        :proxy="state?.proxy"
        :loading="loading"
        @navigate="onNavigate"
        @open-external="openExternal"
        @changed="onScheduleChanged"
        @reload="load"
      />
    </el-aside>

    <el-container>
      <el-header class="app-header">
        <button
          type="button"
          class="icon-btn"
          :title="headerBtnTitle"
          @click="toggleSidebar"
        >
          <PanelLeft :size="14" />
        </button>
        <span class="page-title">{{ currentTitle }}</span>
      </el-header>

      <el-main class="app-main">
        <div class="content-scroll">
          <template v-if="state">
            <BaseConfig v-if="activeView === 'base'" :config="state.config" :options="state.options" @change="markDirty" />
            <Accounts v-if="activeView === 'accounts'" :accounts="state.config.accounts" @change="markDirty" @refresh="load" />
            <Tunnel v-if="activeView === 'tunnel' && isConfigMode" :proxy="state.proxy" @change="markDirty" />
            <Summary v-if="activeView === 'summary'" :data="state" :status="statusInfo" :schedule="state.schedule" @clean="cleanOrphans" @copy="copyEnv" @open="openEnvDir" @cancel="scheduleCancel" @reregister="scheduleReregister" />
            <RunLog v-if="activeView === 'runlog' && !isConfigMode" />
            <iframe
              v-if="activeResource"
              class="embed-frame"
              :src="activeResource.url"
              referrerpolicy="no-referrer"
            />
          </template>
          <div v-else-if="error" class="empty-state">
            <p class="empty-desc">{{ error }}</p>
            <button class="action-btn secondary" @click="load">重试</button>
          </div>
          <div v-else class="empty-state">
            <div class="skeleton-row" v-for="i in 5" :key="i" :style="{ animationDelay: i * 0.08 + 's' }" />
          </div>
        </div>
      </el-main>
    </el-container>
  </el-container>

  <el-drawer
    v-model="drawer"
    class="sidebar-drawer"
    direction="ltr"
    size="240px"
    :with-header="false"
  >
    <SidebarContent
      :nav-items="visibleNavItems"
      :resource-items="resourceItems"
      :active-view="activeView"
      :schedule="state?.schedule"
      :proxy="state?.proxy"
      :loading="loading"
      @navigate="onDrawerNavigate"
      @open-external="openExternal"
      @changed="onScheduleChanged"
      @reload="load"
    />
  </el-drawer>
</template>

<script setup>
import { computed, h, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  BookOpen,
  CalendarDays,
  GlobeCode,
  MessageSquareShare,
  MessagesSquare,
  PanelLeft,
  Play,
  Sparkles,
  Users,
} from '@lucide/vue'
import { py, on } from './api'
import BaseConfig from './views/BaseConfig.vue'
import Accounts from './views/Accounts.vue'
import Tunnel from './views/Tunnel.vue'
import Summary from './views/Summary.vue'
import RunLog from './views/RunLog.vue'
import SidebarContent from './views/SidebarContent.vue'

const BREAKPOINT = 768

const state = ref(null)
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const dirty = ref(false)
const lastSaved = ref('')
const activeView = ref('summary')
let saveTimer = null

const isNarrow = ref(false)
const asideCollapsed = ref(false)
const drawer = ref(false)

const GithubIcon = {
  render() {
    return h(
      'svg',
      { viewBox: '0 0 1024 1024', xmlns: 'http://www.w3.org/2000/svg', fill: 'currentColor' },
      [
        h('path', {
          d: 'M695.744 981.312H419.712c-26.816 0-47.296-21.056-47.296-48.64V739.84c0-29.184 6.336-56.768 17.344-81.088-129.28-48.64-214.528-150.72-214.528-264.256 0-53.44 17.344-103.68 50.496-147.52C213.12 204.8 209.92 151.296 213.12 89.664c3.2-27.52 23.68-46.976 48.896-46.976 14.208 0 134.08 1.6 203.52 64.832a504.384 504.384 0 0 1 184.512 0C717.888 44.288 837.696 42.688 853.504 42.688a47.36 47.36 0 0 1 47.36 45.376c4.672 61.568 0 115.072-12.672 157.248 33.152 45.44 50.496 95.68 50.496 147.52 0 113.472-85.184 215.68-214.528 264.32 11.072 25.856 17.344 53.44 17.344 81.024v192.896c1.6 27.52-20.48 50.24-45.76 50.24z m-228.672-97.28h181.376V739.84c0-27.52-12.608-53.504-33.152-71.36a48.96 48.96 0 0 1-15.744-50.24c4.736-17.856 18.944-32.384 36.288-35.648 123.008-24.32 208.192-102.144 208.192-189.696 0-46.976-25.216-82.688-45.76-105.344a47.36 47.36 0 0 1-7.872-55.168c6.336-12.928 15.808-40.512 15.808-89.152-39.488 6.464-85.184 19.456-102.528 45.44a47.552 47.552 0 0 1-50.56 19.392 393.024 393.024 0 0 0-193.92 0c-18.944 4.864-37.888-3.2-50.56-19.456-17.28-25.92-63.04-38.912-102.464-45.376 1.6 48.64 9.472 76.16 15.744 89.152a53.696 53.696 0 0 1-7.872 55.168c-20.48 22.656-45.76 58.368-45.76 105.344 0 87.552 85.184 163.776 208.256 189.696 17.28 3.2 31.552 17.792 36.224 35.648a51.008 51.008 0 0 1-15.744 50.24c-20.48 17.856-33.152 43.776-33.152 71.36v144.256h3.2z',
        }),
        h('path', {
          d: 'M403.968 788.416c-212.928 0-309.12-194.56-313.92-202.688-11.008-24.32-1.536-53.44 20.48-64.832a47.424 47.424 0 0 1 63.168 21.12c3.136 6.4 80.448 157.248 241.28 149.12a46.08 46.08 0 0 1 48.896 47.04c1.6 27.52-18.88 50.24-45.696 50.24h-14.208z',
        }),
      ],
    )
  },
}

const navItems = [
  { id: 'summary', label: '概览', icon: Sparkles },
  { id: 'accounts', label: '账户配置', icon: Users },
  { id: 'base', label: '任务配置', icon: Play },
  { id: 'runlog', label: '执行日志', icon: CalendarDays },
  { id: 'tunnel', label: '隧道配置', icon: GlobeCode },
]

const resourceItems = [
  { id: 'res-doc', label: '文档', icon: BookOpen, url: 'https://oilu.cn/DouYinSparkFlow/#/README', embed: true },
  { id: 'res-discuss', label: '讨论区', icon: MessagesSquare, url: 'https://github.com/2061360308/DouYinSparkFlow/discussions', embed: false },
  { id: 'res-github', label: 'GitHub', icon: GithubIcon, url: 'https://github.com/2061360308/DouYinSparkFlow', embed: false },
  { id: 'res-feedback', label: '反馈', icon: MessageSquareShare, url: 'https://github.com/2061360308/DouYinSparkFlow/issues', embed: false },
]

// 「生成配置」模式：显示隧道配置、隐藏执行日志；本机执行模式反之
const isConfigMode = computed(() => state.value?.schedule?.mode === 'config')

const visibleNavItems = computed(() => {
  const mode = state.value?.schedule?.mode
  return navItems.filter((item) => {
    if (item.id === 'tunnel') return mode === 'config'
    if (item.id === 'runlog') return mode !== 'config'
    return true
  })
})

const showAside = computed(() => !isNarrow.value && !asideCollapsed.value)

const activeResource = computed(
  () => resourceItems.find((item) => item.id === activeView.value) || null,
)

const currentTitle = computed(() => {
  const all = [...navItems, ...resourceItems]
  return all.find((item) => item.id === activeView.value)?.label || ''
})

const headerBtnTitle = computed(() => {
  if (isNarrow.value) return '打开菜单'
  return asideCollapsed.value ? '展开侧边栏' : '收起侧边栏'
})

const saveStatus = computed(() => {
  if (saving.value) return '保存中…'
  return lastSaved.value ? `已保存 ${lastSaved.value}` : '就绪'
})

const envPathLabel = computed(() => {
  const path = state.value?.env_path ?? ''
  return path ? `.env  ${path}` : ''
})

const issueText = computed(() => {
  const issues = state.value?.issues ?? []
  const errors = issues.filter(([level]) => level === '错误').length
  const warnings = issues.filter(([level]) => level === '警告').length
  if (errors) return `${errors} 个错误 / ${warnings} 个警告`
  if (warnings) return `${warnings} 个警告`
  return '校验通过'
})

const issueClass = computed(() => {
  const issues = state.value?.issues ?? []
  if (issues.some(([level]) => level === '错误')) return 'err'
  if (issues.some(([level]) => level === '警告')) return 'warn'
  return 'ok'
})

const statusInfo = computed(() => ({
  envPath: envPathLabel.value,
  issueText: issueText.value,
  issueClass: issueClass.value,
  saveStatus: saveStatus.value,
  run: state.value?.schedule?.run ?? {},
}))

function updateNarrow() {
  const narrow = window.innerWidth < BREAKPOINT
  if (narrow !== isNarrow.value) {
    isNarrow.value = narrow
    if (!narrow) drawer.value = false
  }
}

function toggleSidebar() {
  if (isNarrow.value) drawer.value = true
  else asideCollapsed.value = !asideCollapsed.value
}

function onNavigate(id) {
  activeView.value = id
  if (id === 'summary') refreshSchedule()
}

function onDrawerNavigate(id) {
  activeView.value = id
  drawer.value = false
  if (id === 'summary') refreshSchedule()
}

async function refreshSchedule() {
  if (!state.value) return
  try {
    state.value.schedule = await py('schedule_status')
  } catch (e) {
    // 状态刷新失败不影响页面
  }
}

async function scheduleCancel() {
  try {
    state.value.schedule = await py('schedule_cancel')
    ElMessage({ type: 'success', message: '已取消系统注册' })
  } catch (e) {
    ElMessage({ type: 'error', message: String(e.message || e) })
  }
}

async function scheduleReregister() {
  const mode = state.value?.schedule?.mode
  if (mode !== 'scheduled' && mode !== 'boot') return
  try {
    const res = await py('schedule_set_mode', { mode })
    state.value.schedule = res.schedule
    state.value.proxy = res.proxy
    ElMessage({ type: 'success', message: '已重新注册系统任务' })
  } catch (e) {
    ElMessage({ type: 'error', message: String(e.message || e) })
  }
}

async function openExternal(url) {
  try {
    await py('open_external_url', { url })
  } catch (e) {
    ElMessage({ type: 'error', message: String(e.message || e) })
  }
}

onMounted(() => {
  updateNarrow()
  window.addEventListener('resize', updateNarrow)
  load()
  on('notify', (data) => ElMessage(data?.tip ?? ''))
})

onUnmounted(() => {
  window.removeEventListener('resize', updateNarrow)
  if (saveTimer) clearTimeout(saveTimer)
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    state.value = await py('get_config')
    dirty.value = false
    for (const note of state.value.notes ?? []) ElMessage({ type: 'info', message: note })
  } catch (e) {
    error.value = String(e.message || e)
    state.value = null
  } finally {
    loading.value = false
  }
}

function markDirty() {
  dirty.value = true
  error.value = ''
  if (saveTimer) clearTimeout(saveTimer)
  saveTimer = setTimeout(save, 500)
}

async function onScheduleChanged() {
  await load()
  if (activeView.value === 'tunnel' && !isConfigMode.value) {
    activeView.value = 'summary'
  }
  if (activeView.value === 'runlog' && isConfigMode.value) {
    activeView.value = 'summary'
  }
}

async function save() {
  if (!state.value || saving.value) return
  saving.value = true
  try {
    const res = await py('save_config', { config: state.value.config, proxy: state.value.proxy })
    lastSaved.value = res.saved_at
    for (const note of res.notes ?? []) ElMessage({ type: 'info', message: note })
    if ((res.orphans ?? []).length)
      ElMessage({ type: 'warning', message: `有 ${res.orphans.length} 个未使用的旧 Cookie 变量，可在「汇总」里清理` })
    await load()
  } catch (e) {
    ElMessage({ type: 'error', message: String(e.message || e) })
  } finally {
    saving.value = false
  }
}

async function cleanOrphans() {
  try {
    const res = await py('clean_orphans')
    ElMessage({ type: 'success', message: `已清理 ${res.removed} 个未使用的 Cookie 变量` })
    await load()
  } catch (e) {
    ElMessage({ type: 'error', message: String(e.message || e) })
  }
}

async function copyEnv() {
  const text = Object.entries(state.value.env_map)
    .map(([key, value]) => `${key}=${value}`)
    .join('\n')
  await navigator.clipboard.writeText(text)
  ElMessage({ type: 'success', message: '已复制 .env 内容' })
}

async function openEnvDir() {
  await py('open_env_dir')
}
</script>

<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html, body, #app {
  height: 100%;
  overflow: hidden;
  font-family: var(--vg-font);
  font-size: 13px;
  line-height: 1.6;
  color: var(--vg-fg);
  background: var(--vg-bg);
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

.app { height: 100%; }

/* ── 侧边栏（el-aside）：浅色 + shadow-as-border ── */
.app-aside {
  background: var(--vg-bg-subtle);
  box-shadow: 1px 0 0 0 var(--vg-border);
  border: none;
}

/* ── 顶栏（el-header） ── */
.app-header {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 48px;
  padding: 0 14px;
  background: var(--vg-bg);
  box-shadow: 0 1px 0 0 var(--vg-border);
}

.icon-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border: none;
  border-radius: var(--vg-radius-sm);
  background: transparent;
  color: var(--vg-fg-2);
  cursor: pointer;
  box-shadow: var(--vg-ring);
  transition: background 0.14s ease, color 0.14s ease;
}

.icon-btn:hover { background: var(--vg-bg-hover); color: var(--vg-fg); }
.icon-btn svg { width: 14px; height: 14px; }

.page-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--vg-fg);
  letter-spacing: -0.01em;
}

/* ── 主区（el-main） ── */
.app-main {
  padding: 0;
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--vg-bg);
}

.content-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 24px 28px 36px;
}

/* ── 资源页嵌入（iframe） ── */
.embed-frame {
  display: block;
  width: 100%;
  height: 100%;
  border: none;
  border-radius: var(--vg-radius);
  background: var(--vg-bg);
  box-shadow: var(--vg-shadow-card);
}

/* ── 抽屉（teleport 到 body） ── */
.sidebar-drawer .el-drawer__body { padding: 0; background: var(--vg-bg-subtle); }
</style>
