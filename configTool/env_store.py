"""读写项目根的 .env（位置见 paths.ENV_FILE：源码运行 = 仓库根，打包运行 = exe 同级）。

写入策略是「就地更新」：只改动本工具管理的那些键，你手写的注释和其他变量原样保留。
（用 python-dotenv 的 set_key / unset_key 实现，它们会保留文件原有结构与注释行。）

这份文件正是主程序真正读的那份配置 —— 所以天然「生成即可用」，
不需要再手工把 .env 搬到别处。
"""

from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key

from configTool.models import Config
from configTool.paths import ENV_FILE

HEADER = """# DouYinSparkFlow 配置文件
#
# 这个文件由本地配置生成器（configTool）自动读写：
#   - 下面那些由工具管理的变量会被就地更新
#   - 你手写的注释和其他变量不会被改动
#
# 要点：
# - TASKS 和 COOKIES_<抖音号> 是必填项，且必须保持单行
# - COOKIES_<抖音号> 的后缀必须与 TASKS 里该账号的 unique_id 完全一致
# - TASKS 里每项的 fingerprint 是该账号固定的浏览器指纹种子（由 configTool 分配，
#   与 profiles.json 里的那份一致），别手改 —— 改了等于让这个账号换设备
# - MESSAGE_TEMPLATE 用 \\n 表示换行
# - 本文件就生成在**项目根目录**（与主程序的 main.py 同级），生成即可用
#     Docker → 复制 / 挂载为 ./config/.env
# - 工具自己的数据（profiles/、profiles.json、local.json）在 configTool/ 下，与本文件无关
"""


def resolve_path(path=None) -> Path:
    return Path(path) if path else ENV_FILE


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def load_config(path=None) -> tuple:
    """返回 (配置, 提示列表)。文件不存在时返回默认配置。

    注意这里**不会**补占位账号：账号信息全部来自浏览器登录，凭空造一个
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
    config = Config.from_env_map(mapping)

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
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(HEADER, encoding="utf-8")
        notes.append(f"已创建 {target.name}")

    managed = config.to_env_map()
    for key, value in managed.items():
        # quote_mode="never"：与 .env.example / index.html 的写法保持一致，不加引号，
        # 否则 Docker 的 env_file 解析器不会还原被转义的 \" ，JSON 值会坏掉。
        set_key(str(target), key, value, quote_mode="never", encoding="utf-8")

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
    removed = 0
    for key in keys:
        try:
            if unset_key(str(target), key, quote_mode="never", encoding="utf-8"):
                removed += 1
        except Exception:
            continue
    return removed


# ---------------------------------------------------------------------------
# 值层面的风险检查
# ---------------------------------------------------------------------------
def risky_values(config: Config) -> list:
    """返回 [(键, 说明)]：值里含 # 或首尾带空白时，.env 解析可能出偏差。"""
    risky: list = []
    for key, value in config.to_env_map().items():
        if "#" in value:
            risky.append((key, "值里含 # ，.env 解析时可能被当成行内注释截断"))
        elif value != value.strip():
            risky.append((key, "值首尾有空白，.env 解析器可能裁掉"))
    return risky
