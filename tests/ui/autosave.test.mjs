// Exercise App.vue's real script with deferred bridge calls and fake timers.
// No browser, account or third-party Node dependencies are needed.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const source = readFileSync(new URL('../../app/web/ui/src/App.vue', import.meta.url), 'utf8')
const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1]
  .replace(/^import[\s\S]*? from ['"][^'"]+['"]\r?$/gm, '')

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function app(py) {
  const timers = new Map(), notices = []
  let id = 0
  const context = {
    py, ref: value => ({ value }), computed: getter => ({ get value() { return getter() } }),
    onMounted() {}, onUnmounted() {}, ElMessage: notice => notices.push(notice),
    setTimeout: callback => { timers.set(++id, callback); return id },
    clearTimeout: key => timers.delete(key),
    BookOpen: {}, CalendarDays: {}, GlobeCode: {}, MessageSquareShare: {},
    MessagesSquare: {}, PanelLeft: {}, Play: {}, Sparkles: {}, Users: {},
  }
  vm.createContext(context)
  vm.runInContext(script + '\nglobalThis.app = { state, dirty, saving, saveStatus, markDirty, save, load, startAiChat };', context)
  context.app.state.value = { config: { message_template: 'first' }, proxy: {} }
  return {
    ...context.app, timers, notices,
    tick() { const [key, callback] = timers.entries().next().value; timers.delete(key); callback() },
  }
}

const flush = () => new Promise(resolve => setImmediate(resolve))

test('an edit whose debounce fires during save is queued, not discarded', async () => {
  const first = deferred(), second = deferred(), saved = []
  const a = app((method, payload) => {
    if (method === 'save_config') { saved.push(payload); return saved.length === 1 ? first.promise : second.promise }
    return Promise.resolve({ config: saved.at(-1).config, proxy: {} })
  })
  a.markDirty()
  const saving = a.save()
  a.state.value.config.message_template = 'second'
  a.markDirty()
  a.tick()
  assert.equal(saved.length, 1)
  assert.equal(saved[0].config.message_template, 'first')
  first.resolve({ saved_at: 'one' })
  await saving
  assert.equal(a.state.value.config.message_template, 'second')
  assert.equal(a.dirty.value, true)
  a.tick()
  assert.equal(saved[1].config.message_template, 'second')
  second.resolve({ saved_at: 'two' })
  await flush()
  assert.equal(a.dirty.value, false)
  assert.equal(a.state.value.config.message_template, 'second')
})

test('edits during post-save reload survive the old server response', async () => {
  const reload = deferred(), saved = []
  const a = app((method, payload) => {
    if (method === 'save_config') { saved.push(payload); return Promise.resolve({ saved_at: 'one' }) }
    return reload.promise
  })
  a.markDirty()
  const saving = a.save()
  await flush()
  a.state.value.config.message_template = 'newer'
  a.markDirty()
  a.tick()
  reload.resolve({ config: { message_template: 'first' }, proxy: {} })
  await saving
  assert.equal(a.state.value.config.message_template, 'newer')
  assert.equal(a.dirty.value, true)
  assert.equal(a.timers.size, 1)
})

test('failed save keeps edits visible and does not retry forever', async () => {
  const a = app(() => Promise.reject(new Error('disk unavailable')))
  a.markDirty()
  await a.save()
  assert.equal(a.dirty.value, true)
  assert.equal(a.saveStatus.value, '未保存')
  assert.equal(a.saving.value, false)
  assert.equal(a.timers.size, 0)
  assert.match(a.notices[0].message, /disk unavailable/)
})

test('a failed refresh retains the current form', async () => {
  const a = app(() => Promise.reject(new Error('bridge unavailable')))
  await a.load()
  assert.equal(a.state.value.config.message_template, 'first')
})

test('out-of-order refresh responses do not revert the latest form', async () => {
  const old = deferred(), latest = deferred()
  let calls = 0
  const a = app(() => ++calls === 1 ? old.promise : latest.promise)
  const first = a.load(), second = a.load()
  latest.resolve({ config: { message_template: 'latest' }, proxy: {} })
  await second
  old.resolve({ config: { message_template: 'old' }, proxy: {} })
  await first
  assert.equal(a.state.value.config.message_template, 'latest')
})

test('AI startup also schedules edits made during its config save', async () => {
  const pending = deferred(), calls = []
  const a = app((method, payload) => {
    calls.push([method, payload])
    return method === 'save_config' ? pending.promise : Promise.resolve()
  })
  a.markDirty()
  const starting = a.startAiChat(() => {})
  a.state.value.config.message_template = 'edited while starting'
  a.markDirty()
  a.tick()
  pending.resolve({ saved_at: 'one' })
  await starting
  assert.equal(a.dirty.value, true)
  assert.equal(a.timers.size, 1)
  assert.equal(calls[0][1].config.message_template, 'first')
  assert.equal(calls[1][0], 'ai_chat_start')
})
