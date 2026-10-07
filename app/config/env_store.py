"""读写项目根的 .env（位置见 paths.ENV_FILE）。

写入策略是「就地更新」：只改动本工具管理的那些键，你手写的注释和其他变量原样保留。
一次生成完整配置再原子替换，保留文件原有结构与注释行。
"""

from __future__ import annotations

from pathlib import Path
import os
import stat
import tempfile
import threading
import time

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from app.config.models import Config
from app.paths import ENV_FILE

HEADER = """# DouYinSparkFlow 配置文件
#
# 这个文件由本地可视化工具（app）自动读写：
#   - 下面那些由工具管理的变量会被就地更新
#   - 你手写的注释和其他变量不会被改动
#
# 要点：
# - TASKS 和 COOKIES_<抖音号> 是必填项，且必须保持单行
# - COOKIES_<抖音号> 的后缀必须与 TASKS 里该账号的 unique_id 完全一致
# - TASKS 里每项的 fingerprint 是该账号固定的浏览器指纹种子（由 app 分配，
#   与 profiles.json 里的那份一致），别手改 —— 改了等于让这个账号换设备
# - MESSAGE_TEMPLATE 用 \\n 表示换行
# - NOTIFY 是任务完成后的通知渠道列表（JSON 数组），由界面的「消息通知」配置
# - 本文件生成在项目根目录，主程序直接用；Docker → 复制 / 挂载为 ./config/.env
# - 工具自己的数据（profiles/、profiles.json、local.json）在 app/ 下
"""

_WRITE_LOCK = threading.RLock()


def _write_updates(target: Path, updates: dict, remove=()) -> int:
    """Batch updates, preserving unknown lines; retry temporary Windows locks."""
    with _WRITE_LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.exists()
        mode = stat.S_IMODE(target.stat().st_mode) if existing else None
        original = target.read_text(encoding="utf-8") if existing else HEADER
        import io
        lines = []
        written = set()
        removed = set()
        for binding in parse_stream(io.StringIO(original)):
            key = binding.key
            if key in remove:
                removed.add(key)
            elif key in updates:
                if key not in written:
                    lines.append(f"{key}={updates[key]}\n")
                    written.add(key)
            else:
                lines.append(binding.original.string)
        content = "".join(lines)
        for key, value in updates.items():
            if key not in written:
                if content and not content.endswith("\n"):
                    content += "\n"
                content += f"{key}={value}\n"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             prefix=".tmp_", dir=target.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if mode is not None:
                os.chmod(temporary, mode)
            for attempt in range(7):
                try:
                    os.replace(temporary, target)
                    break
                except PermissionError as exc:
                    if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 6:
                        raise
                    time.sleep(min(0.05 * 2 ** attempt, 0.5))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return len(removed)


def resolve_path(path=None) -> Path:
    return Path(path) if path else ENV_FILE


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def load_config(path=None) -> tuple:
    """返回 (配置, 提示列表)。文件不存在时返回默认配置。

    注意这里不会补占位账号：账号信息全部来自浏览器登录，凭空造一个
    「账号1」既没有抖音号也没有 Cookie，只会让界面显示一条假数据、还让校验
    一路报错。没有账号就返回空列表，界面会显示添加引导。
    """
    target = resolve_path(path)
    notes: list = []

    if not target.exists():
        notes.append(f"未找到 {target.name}，已载入默认值（首次保存时自动创建）")
        return Config(), notes

    try:
        raw = dotenv_values(target)
    except Exception as exc:
        notes.append(f"读取 {target.name} 失败：{type(exc).__name__}: {exc}")
        return Config(), notes

    mapping = {key: value for key, value in raw.items() if value is not None}
    try:
        config = Config.from_env_map(mapping)
    except (ValueError, TypeError):
        # 不让坏的 AI 配置阻止用户打开界面修复，原 .env 不会被自动改写。
        mapping.pop("AI_CHAT", None)
        config = Config.from_env_map(mapping)
        notes.append("AI_CHAT 配置无效，已载入默认陪聊配置；请检查后重新保存")

    if not config.accounts:
        notes.append("TASKS 里没有账号 —— 点「＋ 添加账号」会自动打开浏览器登录")
    else:
        notes.append(f"已从 {target.name} 载入 {len(config.accounts)} 个账户")
    return config, notes


def read_keys(path=None) -> list:
    """只取键名，用于检测残留的 COOKIES_*。"""
    target = resolve_path(path)
    if not target.exists():
        return []
    try:
        return [key for key, value in dotenv_values(target).items() if value is not None]
    except Exception:
        return []


def read_env_map(path=None) -> dict:
    """原始 KEY -> VALUE（值缺失的键丢掉）。调度模块读 CRON_*/TZ 用。"""
    target = resolve_path(path)
    if not target.exists():
        return {}
    try:
        raw = dotenv_values(target)
    except Exception:
        return {}
    return {key: value for key, value in raw.items() if value is not None}


def orphan_cookie_keys(config: Config, path=None) -> list:
    """找出 .env 里存在、但当前账户已不再引用的 COOKIES_*。

    典型场景是改了抖音号之后，旧的 COOKIES_xxx 留在了文件里。这里只报告，
    删不删交给用户决定——避免误删他刻意保留的东西。
    """
    desired = {account.cookies_key for account in config.accounts if account.unique_id.strip()}
    return sorted(
        key
        for key in read_keys(path)
        if key.startswith("COOKIES_") and key not in desired
    )


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------
def save_config(config: Config, path=None) -> tuple:
    """就地写入。返回 (提示列表, 残留的 COOKIES_* 列表)。"""
    target = resolve_path(path)
    notes: list = []

    if not target.exists():
        notes.append(f"已创建 {target.name}")

    managed = config.to_env_map()
    # 保持不加引号，与 Docker env_file 的 JSON 读取方式一致。
    _write_updates(target, managed)

    orphans = sorted(
        key
        for key in read_keys(target)
        if key.startswith("COOKIES_") and key not in managed
    )
    if orphans:
        notes.append(f"检测到 {len(orphans)} 个未使用的旧 Cookie 变量：{', '.join(orphans)}")

    return notes, orphans


def unset_keys(keys, path=None) -> int:
    """删除指定键，返回实际删除的个数。"""
    target = resolve_path(path)
    if not target.exists():
        return 0
    return _write_updates(target, {}, set(keys))
