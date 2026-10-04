<template>
  <div class="accounts-page">
    <div class="page-head">
      <button class="add-btn" :disabled="running" @click="startAdd">
        ＋ 添加账号
      </button>
    </div>

    <div v-if="accounts.length === 0" class="empty-state">
      <p class="empty-title">还没有任何账号</p>
      <p class="empty-sub">点「＋ 添加账号」会打开浏览器完成登录，登录信息会自动抓取并保存。也可以把已有账号的信息直接编辑保存。</p>
    </div>

    <template v-else>
      <div v-for="(account, index) in accounts" :key="index" class="account-card">
        <div class="card-head">
          <span class="card-label">{{ account.username || '未知账号' }}</span>
          <span class="card-id">{{ account.unique_id || '无抖音号' }}</span>
          <div class="head-actions">
            <button class="mini-btn" :disabled="running" @click="startRefresh(account)">刷新登录信息</button>
            <button class="mini-btn" :disabled="running" @click="startConversations(account)">拉取会话列表</button>
            <button class="remove-btn" @click="removeAt(index)">移除</button>
          </div>
        </div>
        <div class="form-grid">
          <div class="field half">
            <label class="label">用户名</label>
            <input v-model="account.username" class="input" @input="emitChange">
          </div>
          <div class="field half">
            <label class="label">抖音号</label>
            <input v-model="account.unique_id" class="input" @input="emitChange">
          </div>
          <div class="field">
            <label class="label">目标好友</label>
            <el-select
              v-model="account.targets"
              class="target-select"
              multiple
              filterable
              allow-create
              default-first-option
              :placeholder="account.conversations?.length ? '从会话列表勾选，或输入新名字回车' : '会话列表拉取后可从下拉勾选，也可以直接输入名字回车'"
              @change="emitChange"
            >
              <el-option
                v-for="name in account.conversations"
                :key="name"
                :label="name"
                :value="name"
              />
            </el-select>
            <p v-if="account.conversations?.length" class="field-hint">
              会话列表共 {{ account.conversations.length }} 个 —— 点开下拉勾选想要的，选中的会以标签显示在输入框里
            </p>
          </div>
          <div class="field">
            <label class="label">Cookies</label>
            <textarea v-model="account.cookies" class="input textarea" rows="3" placeholder='单行 JSON 数组，对应 .env 的 COOKIES_抖音号' @input="emitChange" />
          </div>
          <div class="field half">
            <label class="label">配置目录</label>
            <input :value="account.profile_folder || '（登录后分配）'" class="input disabled" disabled>
          </div>
          <div class="field half">
            <label class="label">浏览器指纹</label>
            <input :value="account.fingerprint ? '--fingerprint=' + account.fingerprint : '（无）'" class="input disabled" disabled>
          </div>
        </div>
      </div>
    </template>

    <BrowserSession
      v-if="activeSession"
      :session="activeSession"
      @saved-login="onSavedLogin"
      @conversations-saved="onConversationsSaved"
      @close="activeSession = null; running = false"
    />
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { py } from '../api'
import BrowserSession from './BrowserSession.vue'

const props = defineProps({
  accounts: { type: Array, required: true },
})
const emit = defineEmits(['change', 'refresh'])

const activeSession = ref(null)
const running = ref(false)

function emitChange() { emit('change') }

function removeAt(index) {
  props.accounts.splice(index, 1)
  ElMessage({ type: 'info', message: '已从列表移除，保存后生效' })
  emit('change')
}

async function startAdd() {
  await openSession('account_login_start', { mode: 'add' }, '添加账号', '')
}

async function startRefresh(account) {
  await openSession(
    'account_login_start',
    { mode: 'refresh', unique_id: account.unique_id },
    '刷新登录信息',
    account.unique_id,
  )
}

async function startConversations(account) {
  await openSession(
    'account_conversations_start',
    { unique_id: account.unique_id },
    '拉取会话列表',
    account.unique_id,
  )
}

async function openSession(method, payload, title, uniqueId) {
  try {
    const res = await py(method, payload)
    if (!res?.ok) throw new Error(res?.error || '启动失败')
    running.value = true
    activeSession.value = {
      id: res.session,
      kind: method === 'account_conversations_start' ? 'conversations' : 'login',
      title,
      uniqueId,
    }
  } catch (e) {
    ElMessage({ type: 'error', message: String(e.message || e) })
  }
}

function onSavedLogin(payload) {
  const account = payload.account
  if (!account?.unique_id) return
  const index = props.accounts.findIndex(
    (a) => a.unique_id && a.unique_id.toLowerCase() === account.unique_id.toLowerCase(),
  )
  if (index >= 0) {
    // 刷新已有账号：保留目标好友，更新其余字段
    const existing = props.accounts[index]
    props.accounts.splice(index, 1, {
      ...existing,
      username: account.username || existing.username,
      unique_id: account.unique_id,
      cookies: account.cookies,
      profile_folder: account.profile_folder || existing.profile_folder,
      fingerprint: account.fingerprint || existing.fingerprint,
    })
    ElMessage({ type: 'success', message: `已更新「${account.unique_id}」的登录信息` })
  } else {
    // 添加新账号
    props.accounts.push({
      ...account,
      targets: [],
    })
    ElMessage({ type: 'success', message: `已添加账号「${account.unique_id}」` })
  }
  emit('change')
}

function onConversationsSaved(_payload) {
  ElMessage({ type: 'success', message: '会话列表已保存，正在刷新…' })
  emit('refresh')
}
</script>

<style scoped>
.accounts-page { width: 100%; }

.page-head {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  margin-bottom: 16px;
}

.add-btn {
  padding: 6px 14px;
  border: none;
  border-radius: 6px;
  background: #6366f1;
  color: #fff;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.12s;
}

.add-btn:hover:not(:disabled) { background: #5558e6; }
.add-btn:disabled { background: #3a3a5c; color: #6c6c8a; cursor: default; }

.empty-state { padding: 24px 0; }
.empty-title { font-size: 14px; font-weight: 600; color: #555; margin-bottom: 6px; }
.empty-sub  { font-size: 12px; color: #888; line-height: 1.5; max-width: 460px; }

.account-card {
  background: #fff;
  border: 1px solid #d4d4d4;
  border-radius: 8px;
  padding: 14px 16px;
  margin-bottom: 14px;
}

.card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid #eee;
}

.card-label { font-weight: 600; font-size: 13px; }
.card-id    { font-size: 12px; color: #888; }

.head-actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
}

.mini-btn {
  padding: 3px 10px;
  border: 1px solid #c8c8c8;
  border-radius: 4px;
  background: #fff;
  color: #444;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.12s;
}

.mini-btn:hover:not(:disabled) { background: #f0f0ff; color: #6366f1; border-color: #a5a8f0; }
.mini-btn:disabled { opacity: 0.45; cursor: default; }

.remove-btn {
  padding: 3px 10px;
  border: 1px solid #d44;
  border-radius: 4px;
  background: transparent;
  color: #d44;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.12s;
}

.remove-btn:hover { background: #d44; color: #fff; }

.form-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 20px;
}

.field { flex: 1 1 100%; min-width: 200px; }
.field.half { flex: 1 1 calc(50% - 10px); min-width: 160px; }

.label {
  display: block;
  font-size: 12px;
  font-weight: 500;
  color: #555;
  margin-bottom: 4px;
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

.input:focus { border-color: #6366f1; box-shadow: 0 0 0 1px rgba(99,102,241,0.18); }
.input::placeholder { color: #aaa; }
.input.disabled { background: #f5f5f5; color: #888; cursor: default; }

.textarea { resize: vertical; line-height: 1.5; min-height: 54px; }

.field-hint {
  font-size: 11.5px;
  color: #888;
  margin: 5px 0 0;
  line-height: 1.5;
}

/* ── 目标好友下拉（el-select）：剥掉网页感，贴近本地控件 ── */
.target-select { width: 100%; }

.target-select :deep(.el-select__wrapper) {
  box-shadow: none;
  border: 1px solid #c8c8c8;
  border-radius: 5px;
  padding: 4px 8px;
  background: #fff;
  font-size: 13px;
  color: #1a1a1a;
  transition: border-color 0.15s;
}

.target-select :deep(.el-select__wrapper:hover) { border-color: #a5a8f0; }

.target-select :deep(.el-select__wrapper.is-focused) {
  border-color: #6366f1;
  box-shadow: 0 0 0 1px rgba(99,102,241,0.18);
}

.target-select :deep(.el-select__selection) {
  min-height: 22px;
  max-height: 120px;
  overflow-y: auto;
}

/* 已选中的标签：蓝底，像原来的手输 tag */
.target-select :deep(.el-tag) {
  background: #eef;
  border-color: #c9c9f5;
  color: #3730a3;
  border-radius: 4px;
  font-size: 12px;
  height: 22px;
  line-height: 20px;
  margin: 2px 4px 2px 0;
}

.target-select :deep(.el-tag .el-tag__close) {
  color: #5b5bd6;
  border-radius: 3px;
}

.target-select :deep(.el-tag .el-tag__close:hover) {
  background: #d44;
  color: #fff;
}

.target-select :deep(.el-select__placeholder) { color: #aaa; line-height: 22px; }

.target-select :deep(.el-select__input) { color: #1a1a1a; font-size: 13px; }

/* 下拉面板里的选项：勾选态用自定义颜色 */
.target-select :deep(.el-select-dropdown__item) {
  font-size: 13px;
  color: #333;
  border-radius: 4px;
  margin: 0 4px;
  min-width: 240px;
}

.target-select :deep(.el-select-dropdown__item.is-selected) {
  color: #6366f1;
  font-weight: 500;
  background: #f4f4ff;
}

.target-select :deep(.el-select-dropdown__item:hover) {
  background: #f0f0ff;
  color: #3730a3;
}

/* 下拉面板本体：稍微收窄、去阴影动画 */
.target-select :deep(.el-select-dropdown) {
  border-radius: 8px;
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.16);
  max-width: 320px;
}
</style>