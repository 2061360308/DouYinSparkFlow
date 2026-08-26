(() => {
  const account = document.querySelector("#task-account");
  const options = document.querySelector("#task-target-options");
  const refresh = document.querySelector("#refresh-targets");
  const status = document.querySelector("#target-status");
  if (!account || !options || !refresh || !status) return;

  async function loadTargets() {
    refresh.disabled = true;
    status.textContent = "正在读取该账号的聊天列表…";
    options.replaceChildren();
    try {
      const response = await fetch(`/accounts/${encodeURIComponent(account.value)}/conversations`, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.message || "好友列表读取失败");
      for (const item of body.items || []) {
        const option = document.createElement("option");
        option.value = item.name;
        options.appendChild(option);
      }
      status.textContent = body.items?.length
        ? `已读取 ${body.items.length} 个最近会话，可直接选择或继续手动输入`
        : "暂未读取到最近会话，仍可手动输入准确昵称";
    } catch (error) {
      status.textContent = error.message || "好友列表读取失败，仍可手动输入";
    } finally {
      refresh.disabled = false;
    }
  }

  account.addEventListener("change", loadTargets);
  refresh.addEventListener("click", loadTargets);
  loadTargets();
})();
