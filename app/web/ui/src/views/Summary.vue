<template>
  <div class="summary-page">
    <div class="status-card">
      <div class="status-row">
        <span class="status-key">保存状态</span>
        <span class="status-val">{{ status.saveStatus || '就绪' }}</span>
      </div>
      <div class="status-row">
        <span class="status-key">配置校验</span>
        <span class="status-val" :class="status.issueClass">{{ status.issueText || '校验通过' }}</span>
      </div>
      <div class="status-row">
        <span class="status-key">配置文件</span>
        <span class="status-val mono" :title="status.envPath">{{ status.envPath || '（未指定）' }}</span>
      </div>
    </div>

    <div v-if="showExecPanel" class="exec-panel">
      <div class="ep-head">
        <span class="ep-title">执行情况</span>
        <span class="ep-mode">{{ schedule.mode_label || schedule.mode }}</span>
        <span class="spacer" />
        <button class="tbtn" @click="$emit(schedule.installed ? 'cancel' : 'reregister')">
          {{ schedule.installed ? '取消' : '重新注册' }}
        </button>
      </div>
      <div class="ep-grid">
        <template v-if="schedule.mode === 'scheduled'">
          <div class="ep-row">
            <span class="ep-key">注册状态</span>
            <span class="ep-val" :class="schedule.installed ? 'ok' : 'warn'">
              {{ schedule.installed ? '已注册' : '未注册' }}
            </span>
          </div>
          <div class="ep-row">
            <span class="ep-key">执行时间</span>
            <span class="ep-val">{{ schedule.run_time ? '每天 ' + schedule.run_time : '（未设置）' }}</span>
          </div>
        </template>
        <div v-else-if="schedule.mode === 'boot'" class="ep-row">
          <span class="ep-key">本次执行</span>
          <span class="ep-val" :class="runClass(schedule.run?.today)">{{ runText(schedule.run?.today) }}</span>
        </div>

        <div class="ep-row">
          <span class="ep-key">今日执行</span>
          <span class="ep-val" :class="runClass(schedule.run?.today)">{{ runText(schedule.run?.today) }}</span>
        </div>
        <div class="ep-row">
          <span class="ep-key">昨日执行</span>
          <span class="ep-val" :class="runClass(schedule.run?.yesterday)">{{ runText(schedule.run?.yesterday) }}</span>
        </div>
      </div>
    </div>

    <div class="toolbar">
      <button class="tbtn" @click="copy">复制 .env 内容</button>
      <button class="tbtn" @click="open">打开程序目录</button>
      <button v-if="(data.orphans ?? []).length" class="tbtn warn" @click="clean">清理 {{ data.orphans.length }} 个旧变量</button>
    </div>

    <div v-if="(data.notes ?? []).length" class="notes">
      <div v-for="(note, i) in data.notes" :key="'n'+i" class="note info">{{ note }}</div>
    </div>

    <div class="section-block">
      <h3 class="block-title">
        .env 汇总（{{ envList.length }} 项）
        <input v-model="filter" class="filter-input" placeholder="过滤变量名">
      </h3>
      <div class="table-wrap">
        <table class="env-table">
          <thead>
            <tr><th class="col-key">变量名</th><th class="col-val">变量值</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in filtered" :key="row.key">
              <td class="col-key">{{ row.key }}</td>
              <td class="col-val" :title="row.value">{{ row.value }}</td>
            </tr>
            <tr v-if="!filtered.length"><td colspan="2" class="empty-row">无匹配</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'

const props = defineProps({
  data: { type: Object, required: true },
  status: { type: Object, default: () => ({}) },
  schedule: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['clean', 'copy', 'open', 'cancel', 'reregister'])
const filter = ref('')

const showExecPanel = computed(
  () => props.schedule.mode === 'scheduled' || props.schedule.mode === 'boot',
)

const envList = computed(() =>
  Object.entries(props.data.env_map ?? {}).map(([key, value]) => ({ key, value: String(value) }))
)

const filtered = computed(() => {
  const kw = filter.value.trim().toUpperCase()
  if (!kw) return envList.value
  return envList.value.filter((item) => item.key.toUpperCase().includes(kw))
})

function copy() { emit('copy') }
function open() { emit('open') }

function runText(entry) {
  if (!entry) return '未执行'
  const time = String(entry.last_at || '').slice(11, 19)
  const status = entry.success ? '成功' : `失败(退出码 ${entry.last_exit_code})`
  return time ? `${status} · ${time}` : status
}

function runClass(entry) {
  if (!entry) return ''
  return entry.success ? 'ok' : 'err'
}

function clean() {
  const count = (props.data.orphans ?? []).length
  if (!count) return
  try {
    if (!window.confirm(`将从 .env 中删除这些不再被任何账户引用的变量：\n\n${props.data.orphans.join('\n')}`)) return
  } catch (e) {
    ElMessage({ type: 'error', message: String(e.message || e) })
    return
  }
  emit('clean')
}
</script>

<style scoped>
.summary-page { width: 100%; }

/* 实时状态 */
.status-card {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 28px;
  background: var(--vg-bg);
  border-radius: var(--vg-radius);
  box-shadow: var(--vg-shadow-card);
  padding: 14px 18px;
  margin-bottom: 16px;
}

.status-row { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
.status-key { font-size: 12px; color: var(--vg-fg-4); flex: 0 0 auto; }
.status-val {
  font-size: 12.5px;
  color: var(--vg-fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 420px;
}
.status-val.mono { font-family: var(--vg-mono); color: var(--vg-fg-2); }
.status-val.ok { color: #0f766e; }
.status-val.warn { color: #92400e; }
.status-val.err { color: #b91c1c; }

/* 执行情况 */
.exec-panel {
  background: var(--vg-bg);
  border-radius: var(--vg-radius);
  box-shadow: var(--vg-shadow-card);
  padding: 14px 18px;
  margin-bottom: 16px;
}

.ep-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.ep-title { font-size: 13px; font-weight: 600; color: var(--vg-fg); }
.ep-mode {
  font-size: 11.5px; padding: 2px 8px; border-radius: 9999px;
  background: #f0f0f0; color: var(--vg-fg-2);
}
.spacer { flex: 1; }

.ep-grid { display: flex; flex-wrap: wrap; gap: 6px 28px; }
.ep-row { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
.ep-key { font-size: 12px; color: var(--vg-fg-4); flex: 0 0 auto; }
.ep-val { font-size: 12.5px; color: var(--vg-fg); }
.ep-val.ok { color: #0f766e; }
.ep-val.warn { color: #92400e; }
.ep-val.err { color: #b91c1c; }

.tbtn {
  padding: 5px 12px; border: none; border-radius: var(--vg-radius-sm);
  background: var(--vg-bg); color: var(--vg-fg); font-size: 12.5px; font-weight: 500;
  cursor: pointer; box-shadow: var(--vg-ring); transition: background 0.14s ease;
}
.tbtn:hover { background: var(--vg-bg-hover); }
.tbtn.warn { color: #b91c1c; box-shadow: 0 0 0 1px rgba(185, 28, 28, 0.25); }
.tbtn.warn:hover { background: #b91c1c; color: #fff; }

/* Toolbar */
.toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }

/* Notes */
.notes { margin-bottom: 16px; }

/* Issues */
.section-block { margin-bottom: 18px; }

.filter-input {
  margin-left: auto; width: 180px;
}

/* Table */
.col-key { width: 240px; }
.col-val { max-width: 0; }
</style>
