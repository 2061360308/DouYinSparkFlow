<template>
  <div class="runlog-page">
    <div class="rl-toolbar">
      <span class="rl-title">最近 7 天</span>
      <el-date-picker
        v-model="selectedDate"
        type="date"
        value-format="YYYY-MM-DD"
        placeholder="选择日期"
        class="date-picker"
        @change="onDateChange"
      />

      <span class="spacer" />

      <div class="legend">
        <span class="lg"><i class="cell none" />未执行</span>
        <span class="lg"><i class="cell ok" />成功</span>
        <span class="lg"><i class="cell err" />失败</span>
      </div>
      <button class="tbtn" @click="refreshAll">刷新</button>
      <button class="tbtn" @click="openLogDir">打开日志目录</button>
    </div>

    <div v-if="historyError" class="rl-warn">{{ historyError }}</div>

    <div class="week-grid">
      <button
        v-for="c in recentCells"
        :key="c.key"
        class="week-cell"
        :class="[cellClass(c.key), { sel: selectedDate === c.key }]"
        @click="selectDay(c.key)"
      >
        <span class="wc-wd">{{ c.isToday ? '今天' : '周' + c.wd }}</span>
        <span class="wc-day">{{ c.date.getMonth() + 1 }}/{{ c.date.getDate() }}</span>
        <span class="wc-state">{{ stateText(c.key) }}</span>
      </button>
    </div>

    <div class="rl-log-block">
      <div class="rl-log-head">
        <span class="rl-log-title">
          {{ selectedDate || '选择某一天' }}
          <span v-if="selectedDate" class="rl-log-state" :class="cellClass(selectedDate)">{{ stateText(selectedDate) }}</span>
        </span>
      </div>
      <div class="rl-log">
        <div v-if="dayLoading" class="rl-empty">读取中…</div>
        <div v-else-if="!selectedDate" class="rl-empty">选择日期查看当天执行日志</div>
        <div v-else-if="!dayText" class="rl-empty">当天无日志</div>
        <pre v-else class="rl-log-text">{{ dayText }}</pre>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { py } from '../api'

const days = ref({})
const historyError = ref('')
const selectedDate = ref('')
const dayText = ref('')
const dayLoading = ref(false)

function pad(n) { return String(n).padStart(2, '0') }
function toKey(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` }
function addDays(d, n) { const x = new Date(d); x.setDate(x.getDate() + n); return x }

function cellClass(key) {
  const e = days.value[key]
  if (!e) return 'none'
  return e.success ? 'ok' : 'err'
}

function stateText(key) {
  const e = days.value[key]
  if (!e) return '未执行'
  const time = String(e.last_at || '').slice(11, 19)
  const s = e.success ? '成功' : `失败(${e.last_exit_code})`
  return time ? `${s} ${time}` : s
}

// 今日为最后一天，往前推 6 天，共 7 天
const recentCells = computed(() => {
  const today = new Date()
  return Array.from({ length: 7 }, (_, i) => {
    const date = addDays(today, i - 6)
    return { key: toKey(date), date, wd: '一二三四五六日'[(date.getDay() + 6) % 7], isToday: i === 6 }
  })
})

async function loadHistory() {
  historyError.value = ''
  try {
    const res = await py('schedule_history')
    days.value = res?.days || {}
    if (res?.error) historyError.value = res.error
  } catch (e) {
    historyError.value = String(e.message || e)
  }
}

async function loadDay(key) {
  if (!key) return
  dayLoading.value = true
  try {
    const res = await py('schedule_day_log', { date: key })
    dayText.value = res?.text || ''
  } catch (e) {
    ElMessage({ type: 'error', message: String(e.message || e) })
  } finally {
    dayLoading.value = false
  }
}

async function selectDay(key) {
  if (!key) return
  selectedDate.value = key
  await loadDay(key)
}

function onDateChange() {
  loadDay(selectedDate.value)
}

async function refreshAll() {
  await loadHistory()
  if (selectedDate.value) await loadDay(selectedDate.value)
}

async function openLogDir() {
  try {
    await py('open_log_dir')
  } catch (e) {
    ElMessage({ type: 'error', message: String(e.message || e) })
  }
}

onMounted(async () => {
  // 切到本页刷新一次：默认选中今天并加载当天日志（不轮询）
  selectedDate.value = toKey(new Date())
  await refreshAll()
})
</script>

<style scoped>
.runlog-page { width: 100%; }

.rl-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.rl-title { font-size: 12.5px; color: var(--vg-fg-2); }
.date-picker { width: 150px; }

.spacer { flex: 1; }

.legend { display: flex; gap: 12px; font-size: 11.5px; color: var(--vg-fg-3); }
.lg { display: inline-flex; align-items: center; gap: 5px; }

.cell { width: 12px; height: 12px; border-radius: 2px; display: inline-block; background: #ebebeb; }
.cell.ok { background: #3fb950; }
.cell.err { background: #f85149; }
.cell.none { background: #ebebeb; }

.tbtn {
  padding: 6px 12px; border: none; border-radius: var(--vg-radius-sm);
  background: var(--vg-bg); color: var(--vg-fg); font-size: 12.5px; font-weight: 500;
  cursor: pointer; box-shadow: var(--vg-ring);
}
.tbtn:hover { background: var(--vg-bg-hover); }

.rl-warn {
  padding: 8px 12px; border-radius: var(--vg-radius-sm); margin-bottom: 12px;
  background: #fef2f2; color: #b91c1c; font-size: 12.5px;
}

/* 最近 7 天 */
.week-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 8px; margin-bottom: 18px; }
.week-cell {
  display: flex; flex-direction: column; align-items: center; gap: 4px;
  padding: 12px 6px; border: none; border-radius: var(--vg-radius);
  background: var(--vg-bg); box-shadow: var(--vg-shadow-card); cursor: pointer;
}
.week-cell:hover { box-shadow: var(--vg-shadow-card-hover); }
.week-cell.ok { background: #eafaf0; }
.week-cell.err { background: #fdeceb; }
.week-cell.sel { outline: 2px solid var(--vg-fg); outline-offset: 1px; }
.wc-wd { font-size: 11.5px; color: var(--vg-fg-3); }
.wc-day { font-size: 15px; font-weight: 600; color: var(--vg-fg); }
.wc-state { font-size: 11px; color: var(--vg-fg-3); text-align: center; }

/* 日志 */
.rl-log-block { margin-top: 6px; }
.rl-log-head { display: flex; align-items: center; margin-bottom: 8px; }
.rl-log-title { font-size: 13px; font-weight: 600; color: var(--vg-fg); }
.rl-log-state { margin-left: 8px; font-size: 12px; font-weight: 500; }
.rl-log-state.ok { color: #0f766e; }
.rl-log-state.err { color: #b91c1c; }
.rl-log-state.none { color: var(--vg-fg-4); }

.rl-log {
  height: 340px; overflow: auto; background: #0a0a0a;
  border-radius: var(--vg-radius); padding: 10px 12px;
}
.rl-empty { color: #6b6b6b; font-size: 12.5px; padding: 4px; }
.rl-log-text {
  margin: 0; color: #d4d4d4; font-family: var(--vg-mono);
  font-size: 11.5px; line-height: 1.65; white-space: pre-wrap; word-break: break-all;
}
</style>
