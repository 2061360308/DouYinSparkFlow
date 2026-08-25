(() => {
  "use strict";

  const root = document.querySelector("[data-account-scan]");
  if (!root) return;

  const TERMINAL = new Set(["succeeded", "failed", "expired", "cancelled"]);
  const csrfToken = root.dataset.csrfToken;
  const startButton = document.querySelector("#scan-start");
  const dialog = document.querySelector("#scan-dialog");
  const closeButton = document.querySelector("#scan-close");
  const cancelButton = document.querySelector("#scan-cancel");
  const stageNode = document.querySelector("#scan-stage");
  const statusNode = document.querySelector("#scan-status");
  const countdownNode = document.querySelector("#scan-countdown");
  const qrNode = document.querySelector("#scan-qr");
  const placeholderNode = document.querySelector("#scan-placeholder");

  let scanId = null;
  let currentStatus = null;
  let timer = null;

  function clearTimer() {
    if (timer !== null) window.clearTimeout(timer);
    timer = null;
  }

  function setQrVisible(visible) {
    qrNode.hidden = !visible;
    placeholderNode.hidden = visible;
    if (!visible) qrNode.removeAttribute("src");
  }

  function setBusy(busy) {
    startButton.disabled = busy;
    startButton.textContent = busy ? "正在发起绑定…" : "扫码绑定抖音账号";
    stageNode.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function showMessage(message, seconds) {
    statusNode.textContent = message || "扫码状态暂不可用";
    countdownNode.textContent = Number.isFinite(seconds) && seconds > 0
      ? `剩余 ${seconds} 秒`
      : "";
  }

  function renderState(state) {
    currentStatus = state.status;
    dialog.dataset.status = state.status;
    showMessage(state.message, state.remaining_seconds);
    const awaitingScan = state.status === "awaiting_scan";
    setQrVisible(awaitingScan);
    if (awaitingScan) {
      qrNode.src = `/accounts/scan/${encodeURIComponent(scanId)}/qr?t=${Date.now()}`;
    }
    const terminal = TERMINAL.has(state.status);
    setBusy(!terminal);
    cancelButton.disabled = terminal;
    if (state.status === "succeeded") {
      window.location.reload();
    }
    return terminal;
  }

  async function readJson(response) {
    if (response.redirected) {
      window.location.assign("/login");
      throw new Error("登录状态已失效");
    }
    let body = {};
    try {
      body = await response.json();
    } catch (_error) {
      body = {};
    }
    if (!response.ok) {
      throw new Error(body.message || "请求未完成，请稍后重试");
    }
    return body;
  }

  async function pollScan() {
    if (!scanId) return;
    try {
      const response = await fetch(`/accounts/scan/${encodeURIComponent(scanId)}`, {
        cache: "no-store",
        credentials: "same-origin",
      });
      const state = await readJson(response);
      if (!renderState(state)) {
        timer = window.setTimeout(pollScan, 2000);
      }
    } catch (error) {
      setBusy(false);
      setQrVisible(false);
      showMessage(error.message, 0);
    }
  }

  async function startScan() {
    clearTimer();
    scanId = null;
    currentStatus = null;
    setQrVisible(false);
    showMessage("正在创建扫码会话", 0);
    setBusy(true);
    cancelButton.disabled = false;
    if (!dialog.open) dialog.showModal();
    try {
      const response = await fetch("/accounts/scan", {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        body: new URLSearchParams({csrf_token: csrfToken}),
      });
      const state = await readJson(response);
      scanId = state.id;
      if (!renderState(state)) {
        timer = window.setTimeout(pollScan, 2000);
      }
    } catch (error) {
      setBusy(false);
      cancelButton.disabled = true;
      showMessage(error.message, 0);
    }
  }

  async function cancelScan() {
    clearTimer();
    if (!scanId || TERMINAL.has(currentStatus)) return;
    try {
      const response = await fetch(
        `/accounts/scan/${encodeURIComponent(scanId)}/cancel`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
          body: new URLSearchParams({csrf_token: csrfToken}),
        },
      );
      const state = await readJson(response);
      renderState(state);
    } catch (error) {
      setBusy(false);
      showMessage(error.message, 0);
    }
  }

  async function requestClose() {
    const active = scanId && !TERMINAL.has(currentStatus);
    if (active && !window.confirm("关闭窗口将取消本次绑定，确定继续吗？")) return;
    if (active) await cancelScan();
    clearTimer();
    dialog.close();
  }

  startButton.addEventListener("click", startScan);
  closeButton.addEventListener("click", requestClose);
  cancelButton.addEventListener("click", async () => {
    await cancelScan();
    dialog.close();
  });
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    requestClose();
  });
})();
