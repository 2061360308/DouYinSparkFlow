<template>
  <div class="config-page">
    <div class="form-grid">
      <div class="field half">
        <label class="label">执行时间（仅 Docker）</label>
        <el-time-picker
          v-model="config.run_time"
          format="HH:mm:ss"
          value-format="HH:mm:ss"
          placeholder="09:00:00"
          class="time-picker"
          @change="emitChange"
        />
      </div>

      <div class="field half">
        <label class="label">输出日志级别</label>
        <select v-model="config.log_level" class="input select" @change="emitChange">
          <option v-for="t in options.log_level_options" :key="t" :value="t">{{ t }}</option>
        </select>
      </div>

      <div class="field">
        <label class="label">一言类型</label>
        <div class="checkbox-row">
          <label v-for="t in options.hitokoto_options" :key="t" class="checkbox-item">
            <input type="checkbox" :value="t" v-model="config.hitokoto_types" @change="emitChange">
            <span>{{ t }}</span>
          </label>
        </div>
      </div>

      <div class="field">
        <label class="label">消息模板</label>
        <textarea v-model="config.message_template" class="input textarea" rows="4" placeholder="用回车换行，写入 .env 时自动转成 \n" @input="emitChange" />
      </div>
    </div>

    <Notifications
      :notifications="config.notifications || []"
      :notify-types="options.notify_types || []"
      @change="emitChange"
    />

    <el-collapse v-model="activeNames" class="advanced-collapse">
      <el-collapse-item title="高级配置" name="advanced">
        <div class="form-grid">
          <div class="field">
            <label class="label">代理地址</label>
            <input v-model="config.proxy_address" class="input" placeholder="留空直连；填了写进 .env 给任务用" @input="emitChange">
          </div>

          <div class="field half">
            <label class="label">时区</label>
            <select v-model="config.tz" class="input select" @change="emitChange">
              <option v-for="t in options.tz_options" :key="t" :value="t">{{ t }}</option>
            </select>
          </div>

          <div class="field third">
            <label class="label">浏览器最长等待（秒）</label>
            <input v-model.number="config.browser_action_timeout" type="number" class="input" :min="ranges.browser_action_timeout?.[0]" :max="ranges.browser_action_timeout?.[1]" @input="emitChange">
          </div>
          <div class="field third">
            <label class="label">扫描总预算（秒）</label>
            <input v-model.number="config.im_scan_timeout" type="number" class="input" :min="ranges.im_scan_timeout?.[0]" :max="ranges.im_scan_timeout?.[1]" @input="emitChange">
          </div>
          <div class="field third">
            <label class="label">门禁等待上限（秒）</label>
            <input v-model.number="config.im_ready_timeout" type="number" class="input" :min="ranges.im_ready_timeout?.[0]" :max="ranges.im_ready_timeout?.[1]" @input="emitChange">
          </div>
          <div class="field third">
            <label class="label">好友列表等待（秒）</label>
            <input v-model.number="config.friend_list_wait_time" type="number" class="input" :min="ranges.friend_list_wait_time?.[0]" :max="ranges.friend_list_wait_time?.[1]" @input="emitChange">
          </div>
          <div class="field third">
            <label class="label">滚动步数上限</label>
            <input v-model.number="config.im_max_steps" type="number" class="input" :min="ranges.im_max_steps?.[0]" :max="ranges.im_max_steps?.[1]" @input="emitChange">
          </div>
          <div class="field third">
            <label class="label">任务重试次数</label>
            <input v-model.number="config.task_retry_times" type="number" class="input" :min="ranges.task_retry_times?.[0]" :max="ranges.task_retry_times?.[1]" @input="emitChange">
          </div>
        </div>
      </el-collapse-item>
    </el-collapse>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import Notifications from './Notifications.vue'

const props = defineProps({
  config: { type: Object, required: true },
  options: { type: Object, required: true },
})
const emit = defineEmits(['change'])

const ranges = computed(() => props.options.ranges ?? {})
const activeNames = ref([]) // 默认折叠

function emitChange() { emit('change') }
</script>

<style scoped>
.config-page { width: 100%; }

.form-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 20px;
}

.field {
  flex: 1 1 100%;
  min-width: 200px;
}

.field.half { flex: 1 1 calc(50% - 10px); min-width: 160px; }
.field.third { flex: 1 1 calc(33.3% - 14px); min-width: 140px; }

.label {
  display: block;
  font-size: 12px;
  font-weight: 500;
  color: #555;
  margin-bottom: 4px;
  line-height: 1.3;
}

.input {
  display: block;
  width: 100%;
  padding: 6px 8px;
  border: 1px solid #c8c8c8;
  border-radius: 5px;
  background: #fff;
  font-size: 13px;
  color: #1a1a1a;
  font-family: inherit;
  outline: none;
  transition: border-color 0.15s;
}

.input:focus {
  border-color: #6366f1;
  box-shadow: 0 0 0 1px rgba(99, 102, 241, 0.18);
}

.input::placeholder { color: #aaa; }

.input:disabled, .input[disabled] {
  background: #f5f5f5;
  color: #888;
  cursor: default;
}

.select {
  appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23666' stroke-width='2.5'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 8px center;
  padding-right: 26px;
  cursor: pointer;
}

.textarea {
  resize: vertical;
  line-height: 1.5;
  min-height: 60px;
}

.checkbox-row {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
}

.checkbox-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 13px;
  color: #333;
  cursor: pointer;
  user-select: none;
}

.checkbox-item input[type="checkbox"] {
  width: 14px;
  height: 14px;
  accent-color: #6366f1;
  cursor: pointer;
}

.time-picker :deep(.el-input__wrapper) {
  border: 1px solid #c8c8c8;
  border-radius: 5px;
  box-shadow: none;
  padding: 0 8px;
  height: 32px;
}

.time-picker :deep(.el-input__wrapper:focus-within) {
  border-color: #6366f1;
  box-shadow: 0 0 0 1px rgba(99, 102, 241, 0.18);
}

.time-picker :deep(.el-input__inner) {
  font-size: 13px;
  font-family: inherit;
  color: #1a1a1a;
}

/* ── 高级配置折叠面板 ── */
.advanced-collapse {
  margin-top: 8px;
  background: var(--vg-bg);
  border-radius: var(--vg-radius);
  box-shadow: var(--vg-shadow-card);
  padding: 0 18px;
}

.advanced-collapse :deep(.el-collapse),
.advanced-collapse :deep(.el-collapse-item__header),
.advanced-collapse :deep(.el-collapse-item__wrap) {
  border: none;
  background: transparent;
}

.advanced-collapse :deep(.el-collapse-item__header) {
  height: 48px;
  font-size: 13px;
  font-weight: 600;
  color: var(--vg-fg);
  letter-spacing: -0.01em;
}

.advanced-collapse :deep(.el-collapse-item__header:hover) { color: var(--vg-fg); }

.advanced-collapse :deep(.el-collapse-item__arrow) { color: var(--vg-fg-3); }

.advanced-collapse :deep(.el-collapse-item__content) {
  padding-bottom: 18px;
  color: inherit;
}
</style>
