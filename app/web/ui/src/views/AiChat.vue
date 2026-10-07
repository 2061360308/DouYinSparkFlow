<template>
  <div class="ai-page">
    <section class="card">
      <div class="card-heading">
        <div><h2>AI 陪聊</h2><p>只回复选定好友的新文字消息。关闭程序会停止陪聊。</p></div>
        <span class="badge" :class="{ live: status.running }">{{ status.stopping ? '正在停止' : status.running ? '运行中' : '未启动' }}</span>
      </div>
      <div class="actions">
        <button class="action-btn primary" :disabled="busy || status.running" @click="start">启动陪聊</button>
        <button class="action-btn secondary" :disabled="!status.running || status.stopping" @click="stop">停止陪聊</button>
        <span class="hint">本次已回复 {{ status.sent || 0 }} 条 · 异常 {{ status.errors || 0 }} 次</span>
      </div>
      <p class="hint">启动时使用当前配置。修改后停止并重新启动即可生效；不会替代原来的续火花任务。</p>
      <p class="hint">启用后，所选好友的新文字消息会发送至你配置的 AI 服务用于生成回复。API 测试只发送一句测试问候。</p>
    </section>

    <section class="card">
      <div class="card-heading"><h2>AI 服务</h2><button class="mini-btn" :disabled="status.running" @click="addProvider">添加服务</button></div>
      <fieldset :disabled="status.running" class="settings">
        <div class="form-grid">
          <div class="field">
            <label class="label" for="ai-provider">当前服务</label>
            <select id="ai-provider" v-model="ai.active_provider" class="input select" @change="change">
              <option v-for="p in ai.providers" :key="p.id" :value="p.id">{{ p.name || '未命名服务' }}</option>
            </select>
          </div>
          <template v-if="provider">
            <div class="field half"><label class="label" for="ai-name">服务名称</label><input id="ai-name" v-model="provider.name" class="input" @input="change"></div>
            <div class="field half"><label class="label" for="ai-kind">接口类型</label><select id="ai-kind" v-model="provider.kind" class="input select" @change="change"><option value="deepseek">DeepSeek</option><option value="openai-compatible">OpenAI 兼容接口</option></select></div>
            <div class="field half"><label class="label" for="ai-url">API 地址</label><input id="ai-url" v-model="provider.base_url" class="input" placeholder="https://api.deepseek.com" @input="change"></div>
            <div class="field half"><label class="label" for="ai-model">模型名称</label><input id="ai-model" v-model="provider.model" class="input" placeholder="deepseek-flash" @input="change"></div>
            <div class="field"><label class="label" for="ai-key">API Key</label><input id="ai-key" v-model="provider.api_key" class="input" type="password" autocomplete="off" placeholder="填入该服务的 API Key" @input="change"><p class="hint">保存在本机 .env 中；概览与复制配置会隐藏密钥，复制到其他环境后需重新填写。</p></div>
          </template>
        </div>
        <div class="actions">
          <button class="mini-btn" :disabled="status.test_busy || busy" @click="test">{{ status.test_busy ? '测试中…' : '测试 API' }}</button>
          <button class="mini-btn danger" :disabled="ai.providers.length <= 1" @click="removeProvider">删除当前服务</button>
        </div>
      </fieldset>
      <p v-if="status.test_result" class="test-result" :class="{ danger: !status.test_result.ok }">{{ status.test_result.ok ? '连接成功：' : '测试失败：' }}{{ status.test_result.text }}</p>
    </section>

    <section class="card">
      <h2>陪聊好友</h2>
      <p class="hint">与续火花好友名单独立。仅支持私聊；重名时请输入抖音号或 UID。</p>
      <p v-if="!config.accounts.length" class="hint">请先在「账户配置」中添加并登录账号。</p>
      <div v-for="account in config.accounts" :key="account.unique_id" class="account-row">
        <label class="label">{{ account.username || account.unique_id }}</label>
        <el-select v-model="account.ai_targets" multiple filterable allow-create default-first-option :disabled="status.running" class="friend-select" placeholder="选择陪聊好友，也可输入抖音号或 UID" @change="change">
          <el-option v-for="name in friendOptions(account)" :key="name" :label="name" :value="name" />
        </el-select>
      </div>
    </section>

    <section class="card">
      <h2>聊天设置</h2>
      <fieldset :disabled="status.running" class="settings">
        <div class="form-grid">
          <div class="field"><label class="label" for="ai-prompt">角色提示词</label><textarea id="ai-prompt" v-model="ai.system_prompt" class="input textarea" rows="4" @input="change" /></div>
          <div v-for="setting in numberSettings" :key="setting.key" class="field half">
            <label class="label" :for="'ai-' + setting.key">{{ setting.label }}</label>
            <input :id="'ai-' + setting.key" v-model.number="ai[setting.key]" class="input" type="number" :min="setting.min" :max="setting.max" @change="change">
          </div>
        </div>
      </fieldset>
      <p class="hint">上下文只在本次运行内存中保留，按账号和好友隔离；图片、视频、表情与群聊暂不回复。</p>
    </section>

    <section class="card"><h2>陪聊状态</h2><p v-if="!status.logs?.length" class="hint">启动后会在这里显示连接和发送结果。</p><div class="logs" aria-live="polite"><div v-for="(log, i) in status.logs" :key="i" class="log" :class="{ danger: log.kind === 'error' }"><time>{{ log.time }}</time><span>{{ log.message }}</span></div></div></section>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { py } from '../api'

const props = defineProps({ config: { type: Object, required: true } })
const emit = defineEmits(['change', 'start'])
const ai = computed(() => props.config.ai_chat)
const provider = computed(() => ai.value.providers.find(p => p.id === ai.value.active_provider))
const busy = ref(false)
const status = ref({ running: false, logs: [], sent: 0, errors: 0 })
let timer
let polling = false
const numberSettings = [
  { key: 'poll_interval', label: '轮询间隔（秒）', min: 2, max: 120 },
  { key: 'cooldown', label: '同一好友回复间隔（秒）', min: 0, max: 3600 },
  { key: 'context_turns', label: '上下文轮数', min: 1, max: 30 },
  { key: 'max_tokens', label: '生成 Token 上限', min: 32, max: 4096 },
  { key: 'max_reply_chars', label: '回复字数上限', min: 20, max: 2000 },
  { key: 'request_timeout', label: 'API 超时（秒）', min: 5, max: 120 },
]
function change() { emit('change') }
function friendOptions(account) { return [...new Set([...(account.conversations || []), ...(account.targets || []), ...(account.ai_targets || [])])] }
function addProvider() {
  const id = `provider-${Date.now()}`
  ai.value.providers.push({ id, name: '新服务', kind: 'openai-compatible', base_url: '', model: '', api_key: '' })
  ai.value.active_provider = id
  change()
}
function removeProvider() {
  ai.value.providers = ai.value.providers.filter(p => p.id !== ai.value.active_provider)
  ai.value.active_provider = ai.value.providers[0].id
  change()
}
async function refresh() {
  if (polling) return
  polling = true
  try { status.value = await py('ai_chat_status') }
  catch (e) { clearInterval(timer); ElMessage.error(e.message) }
  finally { polling = false }
}
async function start() {
  busy.value = true
  // 父组件负责完成保存后再启动，避免读取自动保存尚未落盘的旧配置。
  emit('start', async (error) => {
    if (error) ElMessage.error(error)
    await refresh()
    busy.value = false
  })
}
async function stop() {
  try { status.value = await py('ai_chat_stop') } catch (e) { ElMessage.error(e.message) }
}
async function test() {
  busy.value = true
  try { await py('ai_chat_test', { ai_chat: ai.value }); await refresh() }
  catch (e) { ElMessage.error(e.message) }
  finally { busy.value = false }
}
onMounted(() => { refresh(); timer = setInterval(refresh, 1000) })
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.ai-page { display: flex; flex-direction: column; gap: 18px; max-width: 1000px; }
.card { border: 1px solid var(--vg-border); border-radius: 10px; padding: 20px; }
.card-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
h2 { font-size: 15px; margin-bottom: 10px; }
p, .hint { color: var(--vg-fg-3); font-size: 12px; line-height: 1.7; margin-top: 8px; }
.actions { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
.badge { padding: 3px 10px; border-radius: 20px; background: var(--vg-bg-subtle); white-space: nowrap; }
.live { color: #15803d; background: #eefbf0; }
.settings { border: 0; padding: 0; min-width: 0; }
.settings:disabled { opacity: .65; }
.account-row { margin-top: 16px; }
.friend-select { width: 100%; }
.test-result { white-space: pre-wrap; padding: 10px; background: var(--vg-bg-subtle); border-radius: 6px; }
.danger { color: #b42318; }
.logs { max-height: 260px; overflow: auto; margin-top: 12px; }
.log { display: flex; gap: 14px; font-size: 12px; padding: 5px 0; }
.log time { flex-shrink: 0; color: var(--vg-fg-3); }
</style>
