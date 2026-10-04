<template>
  <div class="notify-section">
    <div class="notify-head">
      <div>
        <div class="notify-title">消息通知</div>
        <div class="notify-desc">任务跑完后把结果推送到这些渠道，可添加多个。</div>
      </div>
      <el-dropdown trigger="click" @command="onAdd">
        <button type="button" class="add-btn">＋ 添加通知方式</button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item v-for="t in notifyTypes" :key="t.id" :command="t.id">
              {{ t.label }}
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>

    <div v-if="!notifications.length" class="notify-empty">
      还没有通知方式 —— 点右上角「＋ 添加通知方式」选一个（如 Bark、Telegram、企业微信）。
    </div>

    <div v-else class="notify-list">
      <div v-for="(item, index) in notifications" :key="index" class="notify-card">
        <div class="notify-card-main">
          <el-switch
            v-model="item.enabled"
            size="small"
            @change="emitChange"
          />
          <div class="notify-card-text">
            <div class="notify-card-name">{{ labelOf(item.type) }}</div>
            <div class="notify-card-summary">{{ summaryOf(item) }}</div>
          </div>
        </div>
        <div class="notify-card-actions">
          <button type="button" class="mini-btn" @click="onEdit(index)">编辑</button>
          <button type="button" class="mini-btn danger" @click="onRemove(index)">删除</button>
        </div>
      </div>
    </div>

    <el-dialog
      v-model="dialog"
      :title="dialogTitle"
      width="460px"
      append-to-body
      align-center
    >
      <div class="dialog-grid">
        <div v-for="f in dialogFields" :key="f.key" class="dialog-field">
          <label class="dialog-label">{{ f.label }}</label>
          <el-switch v-if="f.type === 'bool'" v-model="draft[f.key]" />
          <el-select
            v-else-if="f.type === 'select'"
            v-model="draft[f.key]"
            size="small"
            style="width: 100%"
          >
            <el-option
              v-for="opt in f.options"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
          <el-input
            v-else
            v-model="draft[f.key]"
            size="small"
            :placeholder="f.placeholder || ''"
            clearable
          />
        </div>
      </div>
      <template #footer>
        <button type="button" class="mini-btn" @click="onTestDraft">测试</button>
        <button type="button" class="mini-btn" @click="dialog = false">取消</button>
        <button type="button" class="mini-btn primary" @click="onSave">保存</button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { py } from '../api'

const props = defineProps({
  notifications: { type: Array, required: true },
  notifyTypes: { type: Array, default: () => [] },
})
const emit = defineEmits(['change'])

const dialog = ref(false)
const editingIndex = ref(-1)
const dialogType = ref('')
const draft = ref({})

const specOf = (id) => props.notifyTypes.find((t) => t.id === id) || null
const labelOf = (id) => specOf(id)?.label || id
const dialogFields = computed(() => specOf(dialogType.value)?.fields || [])
const dialogTitle = computed(() => `配置 · ${labelOf(dialogType.value)}`)

function defaultDraft(typeId) {
  const spec = specOf(typeId)
  const data = { type: typeId, enabled: true }
  for (const f of spec?.fields || []) {
    if (f.type === 'bool') data[f.key] = Boolean(f.default)
    else data[f.key] = f.default ?? ''
  }
  return data
}

function summaryOf(item) {
  const spec = specOf(item.type)
  if (!spec) return ''
  const parts = []
  for (const f of spec.fields) {
    if (f.type === 'bool') continue
    const value = item[f.key]
    if (value) parts.push(`${f.label}=${value}`)
  }
  return parts.join('  ')
}

function onAdd(typeId) {
  editingIndex.value = -1
  dialogType.value = typeId
  draft.value = defaultDraft(typeId)
  dialog.value = true
}

function onEdit(index) {
  editingIndex.value = index
  dialogType.value = props.notifications[index].type
  draft.value = { ...props.notifications[index] }
  dialog.value = true
}

function onRemove(index) {
  props.notifications.splice(index, 1)
  emitChange()
}

function onSave() {
  const spec = specOf(dialogType.value)
  const missing = (spec?.fields || [])
    .filter((f) => f.required && f.type !== 'bool' && !String(draft.value[f.key] ?? '').trim())
    .map((f) => f.label)
  if (missing.length) {
    ElMessage({ type: 'warning', message: `请填写：${missing.join('、')}` })
    return
  }
  if (editingIndex.value >= 0) {
    props.notifications.splice(editingIndex.value, 1, { ...draft.value })
  } else {
    props.notifications.push({ ...draft.value })
  }
  dialog.value = false
  emitChange()
}

async function onTestDraft() {
  try {
    await py('notify_test', { notification: { ...draft.value, enabled: true } })
    ElMessage({ type: 'success', message: '测试通知已发送' })
  } catch (e) {
    ElMessage({ type: 'error', message: String(e.message || e) })
  }
}

function emitChange() {
  emit('change')
}
</script>

<style scoped>
.notify-section {
  margin-top: 8px;
  background: var(--vg-bg);
  border-radius: var(--vg-radius);
  box-shadow: var(--vg-shadow-card);
  padding: 14px 18px 16px;
}

.notify-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.notify-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--vg-fg);
}

.notify-desc {
  font-size: 12px;
  color: var(--vg-fg-3);
  margin-top: 2px;
}

.add-btn {
  border: 1px solid var(--vg-border);
  background: var(--vg-bg);
  color: var(--vg-fg);
  font-size: 12px;
  padding: 5px 12px;
  border-radius: var(--vg-radius-sm);
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.14s ease;
}

.add-btn:hover { background: var(--vg-bg-hover); }

.notify-empty {
  margin-top: 12px;
  font-size: 12px;
  color: var(--vg-fg-3);
  line-height: 1.6;
}

.notify-list {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.notify-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 12px;
  border: 1px solid var(--vg-border);
  border-radius: var(--vg-radius-sm);
}

.notify-card-main {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.notify-card-text { min-width: 0; }

.notify-card-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--vg-fg);
}

.notify-card-summary {
  font-size: 11px;
  color: var(--vg-fg-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.notify-card-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

.mini-btn {
  border: 1px solid var(--vg-border);
  background: var(--vg-bg);
  color: var(--vg-fg);
  font-size: 12px;
  padding: 4px 10px;
  border-radius: var(--vg-radius-sm);
  cursor: pointer;
  transition: background 0.14s ease;
}

.mini-btn:hover { background: var(--vg-bg-hover); }
.mini-btn.danger { color: #dc2626; }
.mini-btn.primary {
  background: var(--vg-fg);
  color: var(--vg-bg);
  border-color: var(--vg-fg);
}

.dialog-grid {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.dialog-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.dialog-label {
  font-size: 12px;
  font-weight: 500;
  color: var(--vg-fg-2);
}
</style>
