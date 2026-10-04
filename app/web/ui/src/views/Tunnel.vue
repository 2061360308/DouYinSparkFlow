<template>
  <div class="tunnel-page">
    <div class="hint-box">
      抓取代理：只在本地打开浏览器抓 Cookie 时接入 gost 隧道，设置存 local.json，不写进 .env、不传云端。
    </div>
    <TunnelForm :proxy="proxy" @change="emit('change')" />
  </div>
</template>

<script setup>
import TunnelForm from './TunnelForm.vue'

const props = defineProps({
  proxy: { type: Object, required: true },
})
const emit = defineEmits(['change'])
</script>

<style scoped>
.tunnel-page { width: 100%; }

.hint-box {
  padding: 10px 12px;
  border-radius: 6px;
  background: #eef4ff;
  color: #3b5998;
  font-size: 12px;
  line-height: 1.5;
  margin-bottom: 16px;
}
</style>
