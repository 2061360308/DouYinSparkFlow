<template>
  <div class="tunnel-form">
    <div class="form-grid">
      <div class="field">
        <label class="label">启用</label>
        <label class="switch-label">
          <input type="checkbox" v-model="proxy.enabled" @change="emitChange" class="switch-input">
          <span class="switch-track"><span class="switch-thumb" /></span>
          <span class="switch-text">{{ proxy.enabled ? '已启用' : '未启用' }}</span>
        </label>
      </div>
      <div class="field">
        <label class="label">隧道地址</label>
        <input v-model="proxy.tunnel" class="input" placeholder="wss://xxx.cn-hangzhou.fcapp.run:443?path=/ws" @input="emitChange">
      </div>
      <div class="field half">
        <label class="label">隧道账号</label>
        <input v-model="proxy.user" class="input" @input="emitChange">
      </div>
      <div class="field half">
        <label class="label">隧道密码</label>
        <input v-model="proxy.password" type="password" class="input" @input="emitChange">
      </div>
      <div class="field">
        <label class="label">gost 程序路径</label>
        <input v-model="proxy.gost_path" class="input" placeholder="留空自动找程序目录下的 gost.exe" @input="emitChange">
      </div>
    </div>
  </div>
</template>

<script setup>
const props = defineProps({
  proxy: { type: Object, required: true },
})
const emit = defineEmits(['change'])
function emitChange() { emit('change') }
</script>

<style scoped>
.form-grid { display: flex; flex-wrap: wrap; gap: 10px 20px; }
.field      { flex: 1 1 100%; min-width: 200px; }
.field.half { flex: 1 1 calc(50% - 10px); min-width: 160px; }

.label {
  display: block; font-size: 12px; font-weight: 500; color: #555; margin-bottom: 4px;
}

.input {
  display: block; width: 100%; padding: 6px 8px;
  border: 1px solid #c8c8c8; border-radius: 5px; background: #fff;
  font-size: 13px; color: #1a1a1a; font-family: inherit; outline: none;
  transition: border-color 0.15s;
}

.input:focus { border-color: #6366f1; box-shadow: 0 0 0 1px rgba(99,102,241,0.18); }
.input::placeholder { color: #aaa; }

/* Toggle switch */
.switch-label { display: inline-flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; }
.switch-input { position: absolute; opacity: 0; width: 0; height: 0; }

.switch-track {
  position: relative; width: 36px; height: 20px;
  border-radius: 10px; background: #d4d4d4; transition: background 0.2s;
}

.switch-thumb {
  position: absolute; top: 2px; left: 2px;
  width: 16px; height: 16px; border-radius: 50%;
  background: #fff; transition: transform 0.2s;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
}

.switch-input:checked + .switch-track { background: var(--vg-fg); }
.switch-input:checked + .switch-track .switch-thumb { transform: translateX(16px); }
.switch-text { font-size: 12.5px; color: var(--vg-fg-2); }
</style>
