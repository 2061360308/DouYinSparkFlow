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
})
const emit = defineEmits(['clean', 'copy', 'open'])
const filter = ref('')

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
