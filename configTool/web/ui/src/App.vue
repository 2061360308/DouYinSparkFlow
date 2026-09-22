<template>
  <div class="app-root">
    <header class="app-header">
      <div class="title">
        <span class="name">DouYinSparkFlow 配置生成器</span>
        <span class="sub">改动通过「保存到 .env」写盘，位置见底部状态栏</span>
      </div>
      <div class="actions">
        <el-button size="small" :loading="loading" @click="load">重新载入</el-button>
        <el-button size="small" type="primary" :loading="saving" @click="save">保存到 .env</el-button>
      </div>
    </header>

    <el-main class="app-main">
      <template v-if="state">
        <el-tabs v-model="activeTab">
          <el-tab-pane label="基础配置" name="base">
            <BaseConfig :config="state.config" :options="state.options" @change="markDirty" />
          </el-tab-pane>
          <el-tab-pane label="账户配置" name="accounts">
            <Accounts :accounts="state.config.accounts" @change="markDirty" />
          </el-tab-pane>
          <el-tab-pane label="隧道配置" name="tunnel">
            <Tunnel :proxy="state.proxy" @change="markDirty" />
          </el-tab-pane>
          <el-tab-pane label="汇总" name="summary">
            <Summary
              :data="state"
              @clean="cleanOrphans"
              @copy="copyEnv"
              @open="openEnvDir"
            />
          </el-tab-pane>
        </el-tabs>
      </template>
      <el-empty v-else-if="error" :description="error">
        <el-button size="small" @click="load">重试</el-button>
      </el-empty>
      <el-skeleton v-else :rows="6" animated />
    </el-main>

    <footer class="app-status">
      <span class="item">{{ envPathLabel }}</span>
      <span class="item issue" :class="issueClass">{{ issueText }}</span>
      <span class="item right">{{ saveStatus }}</span>
    </footer>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { py, on } from './api'
import BaseConfig from './views/BaseConfig.vue'
import Accounts from './views/Accounts.vue'
import Tunnel from './views/Tunnel.vue'
import Summary from './views/Summary.vue'

const state = ref(null)
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const dirty = ref(false)
const lastSaved = ref('')
const activeTab = ref('base')

const saveStatus = computed(() => {
  if (saving.value) return '保存中…'
  if (dirty.value) return '有未保存的改动…'
  return lastSaved.value ? `已保存 ${lastSaved.value}` : '尚未保存'
})

const envPathLabel = computed(() => {
  const path = state.value?.env_path ?? ''
  return path ? `.env: ${path}` : ''
})

const issueText = computed(() => {
  const issues = state.value?.issues ?? []
  const errors = issues.filter(([level]) => level === '错误').length
  const warnings = issues.filter(([level]) => level === '警告').length
  if (errors) return `校验：${errors} 个错误 / ${warnings} 个警告`
  if (warnings) return `校验：${warnings} 个警告`
  return '校验通过'
})

const issueClass = computed(() => {
  const issues = state.value?.issues ?? []
  if (issues.some(([level]) => level === '错误')) return 'is-error'
  if (issues.some(([level]) => level === '警告')) return 'is-warn'
  return 'is-ok'
})

onMounted(() => {
  load()
  on('notify', (data) => ElMessage(data?.tip ?? ''))
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
}

async function save() {
  if (!state.value) return
  saving.value = true
  try {
    const res = await py('save_config', {
      config: state.value.config,
      proxy: state.value.proxy,
    })
    lastSaved.value = res.saved_at
    for (const note of res.notes ?? []) ElMessage({ type: 'info', message: note })
    if ((res.orphans ?? []).length) {
      ElMessage({
        type: 'warning',
        message: `有 ${res.orphans.length} 个未使用的旧 Cookie 变量，可在「汇总」里清理`,
      })
    }
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
html,
body,
#app {
  height: 100%;
  margin: 0;
  background: #f5f7fa;
}

.app-root {
  display: flex;
  flex-direction: column;
  height: 100%;
  font-family: 'Microsoft YaHei UI', 'Segoe UI', system-ui, sans-serif;
}

.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
}

.app-header .name {
  font-size: 16px;
  font-weight: 700;
  margin-right: 10px;
}

.app-header .sub {
  font-size: 12px;
  color: #909399;
}

.app-main {
  flex: 1;
  min-height: 0;
  padding: 0 16px;
  overflow: hidden;
}

.app-main :deep(.el-tabs) {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.app-main :deep(.el-tabs__content) {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.app-status {
  display: flex;
  align-items: center;
  padding: 8px 20px;
  background: #fff;
  border-top: 1px solid #e4e7ed;
  font-size: 12px;
  color: #909399;
  gap: 16px;
}

.app-status .right {
  margin-left: auto;
}

.app-status .issue.is-ok {
  color: #0f6e56;
}

.app-status .issue.is-warn {
  color: #854f0b;
}

.app-status .issue.is-error {
  color: #a32d2d;
}
</style>