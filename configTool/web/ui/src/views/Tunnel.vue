<template>
  <div class="page-wrap">
    <el-alert
      type="info"
      :closable="false"
      title="抓取代理：只在本地打开浏览器抓 Cookie 时接入云函数侧 gost 隧道，设置存在 local.json，不写进 .env、不传到云端。"
    />
    <el-form :model="proxy" label-width="130px" class="form">
      <el-form-item label="启用">
        <el-switch v-model="proxy.enabled" @change="emitChange" />
      </el-form-item>
      <el-form-item label="隧道地址">
        <el-input
          v-model="proxy.tunnel"
          placeholder="wss://xxx.cn-hangzhou.fcapp.run:443?path=/ws"
          @input="emitChange"
        />
      </el-form-item>
      <el-form-item label="隧道账号">
        <el-input v-model="proxy.user" @input="emitChange" />
      </el-form-item>
      <el-form-item label="隧道密码">
        <el-input v-model="proxy.password" type="password" show-password @input="emitChange" />
      </el-form-item>
      <el-form-item label="gost 程序路径">
        <el-input
          v-model="proxy.gost_path"
          placeholder="留空自动找程序目录下的 gost.exe（gost/ 或 bin/ 子目录也行）"
          @input="emitChange"
        />
      </el-form-item>
    </el-form>
  </div>
</template>

<script setup>
const props = defineProps({
  proxy: { type: Object, required: true },
})
const emit = defineEmits(['change'])

function emitChange() {
  emit('change')
}
</script>

<style scoped>
.page-wrap {
  max-width: 760px;
  padding: 18px 8px 24px;
}
.form {
  margin-top: 14px;
}
</style>