<template>
  <div class="page-wrap">
    <el-form :model="config" label-width="200px" class="form">
      <el-form-item label="代理地址">
        <el-input v-model="config.proxy_address" placeholder="写进 .env 交给云函数跑任务用" />
      </el-form-item>
      <el-form-item label="执行时间（仅 Docker 有效）">
        <el-time-picker
          v-model="config.run_time"
          format="HH:mm:ss"
          value-format="HH:mm:ss"
          placeholder="09:00:00"
          @change="emitChange"
        />
      </el-form-item>
      <el-form-item label="时区">
        <el-select v-model="config.tz" @change="emitChange">
          <el-option v-for="t in options.tz_options" :key="t" :label="t" :value="t" />
        </el-select>
      </el-form-item>
      <el-form-item label="消息模板">
        <el-input
          v-model="config.message_template"
          type="textarea"
          :rows="5"
          placeholder="用回车换行即可，写入 .env 时自动转成 \\n"
          @input="emitChange"
        />
      </el-form-item>
      <el-form-item label="一言类型">
        <el-checkbox-group v-model="config.hitokoto_types" @change="emitChange">
          <el-checkbox v-for="t in options.hitokoto_options" :key="t" :value="t">{{ t }}</el-checkbox>
        </el-checkbox-group>
      </el-form-item>
      <el-form-item label="浏览器操作最长等待时间（秒）">
        <el-input-number
          v-model="config.browser_action_timeout"
          :min="ranges.browser_action_timeout[0]"
          :max="ranges.browser_action_timeout[1]"
          :step="10"
          @change="emitChange"
        />
      </el-form-item>
      <el-form-item label="扫描总预算（秒）">
        <el-input-number
          v-model="config.im_scan_timeout"
          :min="ranges.im_scan_timeout[0]"
          :max="ranges.im_scan_timeout[1]"
          :step="10"
          @change="emitChange"
        />
      </el-form-item>
      <el-form-item label="门禁等待上限（秒）">
        <el-input-number
          v-model="config.im_ready_timeout"
          :min="ranges.im_ready_timeout[0]"
          :max="ranges.im_ready_timeout[1]"
          :step="5"
          @change="emitChange"
        />
      </el-form-item>
      <el-form-item label="好友列表等待时间（秒）">
        <el-input-number
          v-model="config.friend_list_wait_time"
          :min="ranges.friend_list_wait_time[0]"
          :max="ranges.friend_list_wait_time[1]"
          :step="1"
          @change="emitChange"
        />
      </el-form-item>
      <el-form-item label="滚动步数上限（步）">
        <el-input-number
          v-model="config.im_max_steps"
          :min="ranges.im_max_steps[0]"
          :max="ranges.im_max_steps[1]"
          :step="50"
          @change="emitChange"
        />
      </el-form-item>
      <el-form-item label="任务重试次数">
        <el-input-number
          v-model="config.task_retry_times"
          :min="ranges.task_retry_times[0]"
          :max="ranges.task_retry_times[1]"
          :step="1"
          @change="emitChange"
        />
      </el-form-item>
      <el-form-item label="输出日志级别">
        <el-select v-model="config.log_level" @change="emitChange">
          <el-option v-for="t in options.log_level_options" :key="t" :label="t" :value="t" />
        </el-select>
      </el-form-item>
    </el-form>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  config: { type: Object, required: true },
  options: { type: Object, required: true },
})
const emit = defineEmits(['change'])

const ranges = computed(() => props.options.ranges ?? {})

function emitChange() {
  emit('change')
}
</script>

<style scoped>
.page-wrap {
  max-width: 760px;
  padding: 18px 8px 24px;
}
</style>