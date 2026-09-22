<template>
  <div class="page-wrap">
    <el-empty v-if="accounts.length === 0" description="还没有任何账号">
      <template #description>
        <p class="empty-title">还没有任何账号</p>
        <p class="empty-sub">
          账号信息由浏览器登录后自动抓取。自动登录（一期）随后补齐；
          现在可以把已有账号的信息直接编辑保存。
        </p>
      </template>
    </el-empty>

    <template v-else>
      <div
        v-for="(account, index) in accounts"
        :key="index"
        class="account-card"
      >
        <div class="card-head">
          <span class="name">{{ account.username || '（未知）' }}</span>
          <span class="id">{{ account.unique_id || '（未知抖音号）' }}</span>
          <el-button
            type="danger"
            plain
            size="small"
            class="remove"
            @click="removeAt(index)"
          >移除账户</el-button>
        </div>

        <el-form :model="account" label-width="130px">
          <el-form-item label="用户名">
            <el-input v-model="account.username" @input="emitChange" />
          </el-form-item>
          <el-form-item label="抖音号">
            <el-input v-model="account.unique_id" @input="emitChange" />
          </el-form-item>
          <el-form-item label="目标好友">
            <el-select
              v-model="account.targets"
              multiple
              filterable
              allow-create
              default-first-option
              placeholder="输入名字回车即可添加；会话列表拉取（一期）后可改为勾选"
              @change="emitChange"
            />
          </el-form-item>
          <el-form-item label="Cookies" class="cookies">
            <el-input
              v-model="account.cookies"
              type="textarea"
              :rows="3"
              placeholder="单行 JSON 数组，对应写进 .env 的 COOKIES_<抖音号>"
              @input="emitChange"
            />
          </el-form-item>
          <el-form-item label="配置目录">
            <el-input :model-value="account.profile_folder || '（还没有，登录后分配）'" disabled />
          </el-form-item>
          <el-form-item label="浏览器指纹">
            <el-input
              :model-value="account.fingerprint ? '--fingerprint=' + account.fingerprint : '（还没有记录）'"
              disabled
            />
          </el-form-item>
        </el-form>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ElMessage } from 'element-plus'

const props = defineProps({
  accounts: { type: Array, required: true },
})
const emit = defineEmits(['change'])

function emitChange() {
  emit('change')
}

function removeAt(index) {
  props.accounts.splice(index, 1)
  ElMessage({ type: 'info', message: '已从列表移除，保存后生效（旧 COOKIES_* 可在「汇总」清理）' })
  emit('change')
}
</script>

<style scoped>
.page-wrap {
  max-width: 760px;
  padding: 18px 8px 24px;
}

.account-card {
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 14px 16px;
  margin-bottom: 16px;
  background: #fff;
}

.card-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.card-head .name {
  font-weight: 700;
}

.card-head .id {
  color: #909399;
  font-size: 12px;
}

.card-head .remove {
  margin-left: auto;
}

.empty-title {
  font-size: 16px;
  font-weight: 700;
  color: #5f5e5a;
  margin: 0 0 8px;
}

.empty-sub {
  color: #909399;
  font-size: 13px;
  margin: 0;
}
</style>