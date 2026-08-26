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
  let startPending = false;
  let closeRequested = false;
  let cancelPending = false;
  let preparedState = null;
  let preloadPromise = null;
  let qrLoadedFor = null;

  function clearTimer() {
    if (timer !== null) window.clearTimeout(timer);
    timer = null;
  }

  function setQrVisible(visible) {
    qrNode.hidden = !visible;
    placeholderNode.hidden = visible;
    if (!visible) {
      qrNode.removeAttribute("src");
      qrLoadedFor = null;
    }
  }

  function loadQrOnce(id) {
    if (!id || qrLoadedFor === id) return;
    qrNode.src = `/accounts/scan/${encodeURIComponent(id)}/qr`;
    qrLoadedFor = id;
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
      loadQrOnce(scanId);
    }
    const terminal = TERMINAL.has(state.status);
    setBusy(!terminal);
    cancelButton.disabled = terminal;
    if (state.status === "succeeded") {
      showMessage("登录成功，正在刷新账号列表", 0);
      setQrVisible(false);
      cancelButton.disabled = true;
      timer = window.setTimeout(() => {
        if (dialog.open) dialog.close();
        window.location.reload();
      }, 900);
    }
    return terminal;
  }

  async function pollPrepared() {
    if (!scanId || startPending || closeRequested || cancelPending) return;
    try {
      const response = await fetch(`/accounts/scan/${encodeURIComponent(scanId)}`, {
        cache: "no-store",
        credentials: "same-origin",
      });
      const state = await readJson(response);
      preparedState = state;
      currentStatus = state.status;
      if (state.status === "awaiting_scan") {
        loadQrOnce(scanId);
        setQrVisible(true);
        setBusy(false);
        return;
      }
      if (TERMINAL.has(state.status)) {
        scanId = null;
        preparedState = null;
        setBusy(false);
        return;
      }
      timer = window.setTimeout(pollPrepared, 1000);
    } catch (_error) {
      scanId = null;
      preparedState = null;
      setBusy(false);
    }
  }

  async function preloadScan() {
    startPending = true;
    setBusy(true);
    try {
      const response = await fetch("/accounts/scan", {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        body: new URLSearchParams({csrf_token: csrfToken}),
      });
      const state = await readJson(response);
      scanId = state.id;
      currentStatus = state.status;
      preparedState = state;
      startPending = false;
      setBusy(false);
      if (state.status === "awaiting_scan") {
        loadQrOnce(scanId);
        setQrVisible(true);
      } else if (!TERMINAL.has(state.status)) {
        timer = window.setTimeout(pollPrepared, 1000);
      }
    } catch (_error) {
      startPending = false;
      scanId = null;
      currentStatus = null;
      preparedState = null;
      setBusy(false);
    }
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
    if (!scanId || closeRequested || cancelPending) return;
    try {
      const response = await fetch(`/accounts/scan/${encodeURIComponent(scanId)}`, {
        cache: "no-store",
        credentials: "same-origin",
      });
      const state = await readJson(response);
      if (closeRequested || cancelPending) return;
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
    if (preloadPromise) {
      await preloadPromise;
      preloadPromise = null;
    }
    if (scanId && preparedState && !TERMINAL.has(preparedState.status)) {
      clearTimer();
      closeRequested = false;
      cancelPending = false;
      if (!dialog.open) dialog.showModal();
      const terminal = renderState(preparedState);
      if (!terminal) timer = window.setTimeout(pollScan, 1000);
      return;
    }
    clearTimer();
    scanId = null;
    currentStatus = null;
    startPending = true;
    closeRequested = false;
    cancelPending = false;
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
      startPending = false;
      const terminal = renderState(state);
      if (closeRequested) {
        if (TERMINAL.has(currentStatus) || await cancelScan()) {
          closeRequested = false;
          dialog.close();
        } else {
          closeRequested = false;
        }
        return;
      }
      if (!terminal) {
        timer = window.setTimeout(pollScan, 2000);
      }
    } catch (error) {
      startPending = false;
      if (closeRequested && !scanId) {
        closeRequested = false;
        dialog.close();
        return;
      }
      setBusy(false);
      cancelButton.disabled = true;
      showMessage(error.message, 0);
    }
  }

  async function cancelScan() {
    clearTimer();
    if (!scanId || TERMINAL.has(currentStatus)) return true;
    if (cancelPending) return false;
    cancelPending = true;
    closeButton.disabled = true;
    cancelButton.disabled = true;
    showMessage("正在取消绑定", 0);
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
      cancelPending = false;
      closeButton.disabled = false;
      renderState(state);
      return true;
    } catch (error) {
      cancelPending = false;
      closeButton.disabled = false;
      cancelButton.disabled = false;
      stageNode.setAttribute("aria-busy", "false");
      showMessage("取消失败，请重试", 0);
      return false;
    }
  }

  async function requestClose(confirmClose) {
    const active = startPending || (scanId && !TERMINAL.has(currentStatus));
    if (!active) {
      dialog.close();
      return;
    }
    if (confirmClose && !window.confirm("关闭窗口将取消本次绑定，确定继续吗？")) return;
    if (closeRequested || cancelPending) return;
    closeRequested = true;
    clearTimer();
    closeButton.disabled = true;
    cancelButton.disabled = true;
    showMessage("正在取消绑定", 0);
    if (startPending) return;
    if (await cancelScan()) {
      closeRequested = false;
      dialog.close();
    } else {
      closeRequested = false;
    }
  }

  startButton.addEventListener("click", startScan);
  closeButton.addEventListener("click", () => requestClose(true));
  cancelButton.addEventListener("click", () => requestClose(false));
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    requestClose(true);
  });
  window.addEventListener("pagehide", () => {
    if (!scanId || TERMINAL.has(currentStatus)) return;
    clearTimer();
    navigator.sendBeacon(
      `/accounts/scan/${encodeURIComponent(scanId)}/cancel`,
      new URLSearchParams({csrf_token: csrfToken}),
    );
    scanId = null;
  });
  if (root.dataset.preload === "true") {
    preloadPromise = preloadScan();
  }
})();
