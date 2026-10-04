import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import App from './App.vue'
import './styles/theme.css'
// 用仓库根 app/logo.png 作窗口图标。Chromium 的 --app= 窗口把页面 favicon
// 同时用作任务栏图标与标题栏图标；Vite 会把它内联成 data URI（singlefile）。
import logoUrl from '../../../logo.png'

const favicon = document.createElement('link')
favicon.rel = 'icon'
favicon.type = 'image/png'
favicon.href = logoUrl
document.head.appendChild(favicon)

createApp(App).use(ElementPlus).mount('#app')
