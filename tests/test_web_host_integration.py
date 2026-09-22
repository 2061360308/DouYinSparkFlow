"""端到端集成测试：真·自带 Chromium（无头）+ 真·构建产物 + 真·$py 桥。

把「页面加载/桥注册/方法调用」整条链路在真实环境下跑一遍。
浏览器或构建产物缺失时跳过（CI 里会先执行 npm build + 下载浏览器）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from configTool import paths
from configTool.web.bridge import Bridge
from configTool.web.service import Service

_DIST = paths.app_dir() / "web" / "dist" / "index.html"
_BROWSER = paths.browser_binary()

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - 依赖不全时跳过
    sync_playwright = None


@unittest.skipUnless(_BROWSER.is_file(), "缺少自带 Chromium")
@unittest.skipUnless(_DIST.is_file(), "缺少前端构建产物 configTool/web/dist/index.html")
class WebHostIntegrationTests(unittest.TestCase):
    def test_ping_and_get_config_through_real_page(self):
        if sync_playwright is None:
            self.fail("playwright 导入失败")
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge()
            service = Service(Path(tmp) / ".env")
            bridge.register_all(
                ping=service.ping,
                get_config=service.get_config,
                save_config=service.save_config,
            )
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    str(Path(tmp) / "profile"),
                    headless=True,
                    executable_path=str(_BROWSER),
                )
                try:
                    page = context.new_page()
                    page.goto(_DIST.as_uri(), wait_until="load")
                    # 桥在页面加载之后暴露（与 host.py 顺序一致）
                    context.expose_function("$py", bridge.call)
                    result = page.evaluate("window.$py('ping', null)")
                    self.assertEqual(result["pong"], True)
                    config = page.evaluate("window.$py('get_config', null)")
                    self.assertEqual(config["config"]["run_time"], "09:00:00")
                    # Vue 应用要自己完成 挂载 → load() → 桥就绪 → 渲染数据 的全过程
                    page.wait_for_function(
                        "() => document.body.innerText.includes('基础配置')"
                    )
                    env_path = str(Path(tmp) / ".env")
                    page.wait_for_function(
                        "path => document.body.innerText.includes(path)",
                        arg=env_path,
                    )
                    # 完整用户流：点「保存到 .env」→ 桥写盘 → 状态栏出现「已保存」
                    page.get_by_role("button", name="保存到 .env").click()
                    page.wait_for_function(
                        "msg => document.body.innerText.includes(msg)",
                        arg="已保存",
                    )
                    self.assertTrue(Path(env_path).is_file())
                    text = Path(env_path).read_text(encoding="utf-8")
                    self.assertIn("MESSAGE_TEMPLATE", text)
                    self.assertIn("TASKS=", text)
                finally:
                    context.close()


if __name__ == "__main__":
    unittest.main()