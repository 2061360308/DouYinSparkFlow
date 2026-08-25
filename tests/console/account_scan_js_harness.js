const fs = require("fs");
const path = require("path");
const vm = require("vm");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return {promise, resolve};
}

function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    redirected: false,
    async json() { return body; },
  };
}

class Element {
  constructor() {
    this.listeners = new Map();
    this.dataset = {};
    this.attributes = new Map();
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.open = false;
    this.closeCount = 0;
  }

  addEventListener(name, listener) {
    this.listeners.set(name, listener);
  }

  emit(name) {
    const event = {preventDefault() { this.defaultPrevented = true; }};
    return this.listeners.get(name)(event);
  }

  setAttribute(name, value) {
    this.attributes.set(name, value);
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  showModal() {
    this.open = true;
  }

  close() {
    this.open = false;
    this.closeCount += 1;
  }
}

function createEnvironment(fetchImpl) {
  const selectors = [
    "[data-account-scan]",
    "#scan-start",
    "#scan-dialog",
    "#scan-close",
    "#scan-cancel",
    "#scan-stage",
    "#scan-status",
    "#scan-countdown",
    "#scan-qr",
    "#scan-placeholder",
  ];
  const elements = Object.fromEntries(selectors.map((selector) => [selector, new Element()]));
  elements["[data-account-scan]"].dataset.csrfToken = "csrf-fixture";
  const timers = new Map();
  let nextTimer = 1;
  global.document = {querySelector(selector) { return elements[selector]; }};
  global.fetch = fetchImpl;
  global.window = {
    clearTimeout(id) { timers.delete(id); },
    confirm() { return true; },
    location: {assign() {}, reload() {}},
    setTimeout(callback, delay) {
      const id = nextTimer++;
      timers.set(id, {callback, delay});
      return id;
    },
  };
  const source = fs.readFileSync(
    path.resolve(__dirname, "../../spark_console/static/account_scan.js"),
    "utf8",
  );
  vm.runInThisContext(source, {filename: "account_scan.js"});
  return {elements, timers};
}

async function flush() {
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));
}

async function pendingIntent(controlSelector) {
  const start = deferred();
  const cancel = deferred();
  const calls = [];
  const {elements, timers} = createEnvironment((url) => {
    calls.push(url);
    return url === "/accounts/scan" ? start.promise : cancel.promise;
  });
  const dialog = elements["#scan-dialog"];
  const startTask = elements["#scan-start"].emit("click");
  await flush();
  const intentTask = elements[controlSelector].emit("click");
  await flush();
  assert(dialog.closeCount === 0, "dialog closed while scan creation was pending");

  start.resolve(response(201, {
    id: "scan-1",
    status: "queued",
    remaining_seconds: 300,
    error: null,
    message: "等待开始扫码",
    account_id: null,
  }));
  await flush();
  assert(calls.includes("/accounts/scan/scan-1/cancel"), "pending close intent did not cancel returned scan id");
  assert(dialog.closeCount === 0, "dialog closed before cancellation completed");

  cancel.resolve(response(200, {
    id: "scan-1",
    status: "cancelled",
    remaining_seconds: 0,
    error: "cancelled",
    message: "扫码已取消",
    account_id: null,
  }));
  await Promise.all([startTask, intentTask]);
  await flush();
  assert(dialog.closeCount === 1, "dialog did not close after successful cancellation");
  assert(timers.size === 0, "polling remained scheduled after pending cancellation");
}

async function activeCancelFailure() {
  let cancelAttempt = 0;
  const {elements, timers} = createEnvironment(async (url) => {
    if (url === "/accounts/scan") {
      return response(201, {
        id: "scan-2",
        status: "queued",
        remaining_seconds: 300,
        error: null,
        message: "等待开始扫码",
        account_id: null,
      });
    }
    cancelAttempt += 1;
    if (cancelAttempt === 1) {
      return response(503, {error: "internal", message: "sensitive server detail"});
    }
    return response(200, {
      id: "scan-2",
      status: "cancelled",
      remaining_seconds: 0,
      error: "cancelled",
      message: "扫码已取消",
      account_id: null,
    });
  });
  const dialog = elements["#scan-dialog"];
  await elements["#scan-start"].emit("click");
  assert(timers.size === 1, "active scan did not schedule its poll");

  await elements["#scan-cancel"].emit("click");
  assert(dialog.closeCount === 0, "dialog closed after cancellation failed");
  assert(dialog.open, "dialog was hidden after cancellation failed");
  assert(elements["#scan-status"].textContent === "取消失败，请重试", "cancellation failure was not fixed user copy");
  assert(!elements["#scan-status"].textContent.includes("sensitive"), "server cancellation detail reached the UI");
  assert(!elements["#scan-cancel"].disabled, "cancel control was not re-enabled for retry");
  assert(timers.size === 0, "hidden polling continued after cancellation failure");

  await elements["#scan-cancel"].emit("click");
  assert(dialog.closeCount === 1, "successful cancellation retry did not close dialog");
}

async function main() {
  const scenario = process.argv[2];
  if (scenario === "pending-close") await pendingIntent("#scan-close");
  else if (scenario === "pending-cancel") await pendingIntent("#scan-cancel");
  else if (scenario === "active-cancel-failure") await activeCancelFailure();
  else throw new Error(`unknown scenario: ${scenario}`);
  process.stdout.write(JSON.stringify({scenario, ok: true}));
}

main().catch((error) => {
  process.stderr.write(`${error.stack}\n`);
  process.exitCode = 1;
});
