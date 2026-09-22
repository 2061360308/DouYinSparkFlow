<template>
  <div class="page-wrap">
    <div class="toolbar">
      <el-button size="small" @click="copy">复制 .env 内容</el-button>
      <el-button size="small" @click="open">打开程序目录</el-button>
      <el-button
        v-if="(data.orphans ?? []).length"
        size="small"
        type="warning"
        @click="clean"
      >清理 {{ data.orphans.length }} 个旧变量</el-button>
      <span class="path">.env: {{ data.env_path }}</span>
    </div>

    <el-alert
      v-for="(note, i) in data.notes ?? []"
      :key="'n' + i"
      type="info"
      :closable="false"
      :title="note"
      class="note"
    />

    <el-card shadow="never" class="block">
      <template #header>校验结果</template>
      <template v-if="(data.issues ?? []).length">
        <el-alert
          v-for="(issue, i) in data.issues"
          :key="'i' + i"
          :type="issue[0] === '错误' ? 'error' : 'warning'"
          :closable="false"
          :title="issue[1]"
          class="note"
        />
      </template>
      <el-empty v-else description="校验通过" :image-size="48" />
    </el-card>

    <el-card shadow="never" class="block">
      <template #header>
        .env 汇总（{{ envList.length }} 项）
        <el-input
          v-model="filter"
          size="small"
          placeholder="过滤变量名"
          clearable
          class="filter"
        />
      </template>
      <el-table :data="filtered" size="small" max-height="420">
        <el-table-column prop="key" label="变量名" width="240" />
        <el-table-column prop="value" label="变量值" :show-overflow-tooltip="true" />
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'

const props = defineProps({
  data: { type: Object, required: true },
})
const emit = defineEmits(['clean', 'copy', 'open'])

const filter = ref('')

const envList = computed(() =>
  Object.entries(props.data.env_map ?? {}).map(([key, value]) => ({ key, value: String(value) })),
)

const filtered = computed(() => {
  const kw = filter.value.trim().toUpperCase()
  if (!kw) return envList.value
  return envList.value.filter((item) => item.key.toUpperCase().includes(kw))
})

function copy() {
  emit('copy')
}

function open() {
  emit('open')
}

function clean() {
  const count = (props.data.orphans ?? []).length
  if (!count) return
  try {
    const keys = props.data.orphans.join('\n')
    if (!window.confirm(`将从 .env 中删除这些不再被任何账户引用的变量：\n\n${keys}`)) return
  } catch (e) {
    ElMessage({ type: 'error', message: String(e.message || e) })
    return
  }
  emit('clean')
}
</script>

<style scoped>
.page-wrap {
  max-width: 900px;
  padding: 18px 8px 24px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.toolbar .path {
  margin-left: auto;
  font-size: 12px;
  color: #909399;
  font-family: Consolas, monospace;
}
.block {
  margin-top: 12px;
}
.note {
  margin: 6px 0;
}
.filter {
  width: 220px;
  float: right;
}
</style>