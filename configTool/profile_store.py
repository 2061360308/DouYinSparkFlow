"""抖音号 <-> 浏览器配置目录 的对照表（profiles.json），顺带存放每个账号的抓取结果。

为什么需要单独一个文件：

  - 每个账号要用自己独立的浏览器配置目录，目录名必须随机（不能拿抖音号当目录名，
    抖音号随时会改，而且大小写会被环境变量名吞掉）。
  - `.env` 是给主程序吃的配置文件，不能往里塞工具自己的字段（多出来的键会污染
    主程序的配置）。
  - 目录名又不能每次启动都重新随机 —— 那样登录态就找不回来了。

于是单独落一份 JSON 放在程序目录下：

    {
      "version": 1,
      "accounts": {
        "12345678901": {
          "folder": "p3f9a2c7b1e04",
          "fingerprint": "73841",
          "nickname": "示例昵称",
          "uid": "10000000000000001",
          "sec_uid": "MS4wLjABAAAA...",
          "created_at": "2026-09-16 11:58:11",
          "updated_at": "2026-09-16 12:05:03",
          "conversations": ["甲同学", "乙同学", "某群聊"],
          "conversations_at": "2026-09-16 13:20:41"
        }
      }
    }

键 = 抖音号（就是 .env 里 ``COOKIES_<抖音号>`` 的后缀）
值 = 该账号的浏览器配置目录名（``profiles/<folder>/``），外加抓取结果

``conversations`` 是「拉取会话列表」抓到的会话名，界面用它给用户直接点选目标好友。
它属于本工具自己的元数据，**不写进 .env**（.env 里只该有主程序认识的键）。

``fingerprint`` 是该账号固定的浏览器指纹种子，打开浏览器时以
``--fingerprint=<种子>`` 传给隐身浏览器。cloakbrowser 默认每次启动都随机一个种子
（config.py::get_default_stealth_args），同一账号指纹天天变，在风控眼里等于
「同一个人不停换设备」；固定下来才像一个稳定的真实用户。
想换指纹，删掉这个字段即可（下次打开浏览器会重新分配并写回）。

这份 profiles.json 是指纹的权威来源。生成 .env 时会把同一个值抄进
``TASKS[].fingerprint``，好让主程序也能用同一个种子；启动时若发现 profiles.json
里查不到（被删过 / 换了机器），会优先采用 .env 里的值而不是另分配一个。

登录成功时写入目录绑定；界面加载 .env 后靠它找回每个账号该用哪个目录。

账号从列表里移除时**不删**这里的条目 —— 配置目录留着，将来重新添加同一个账号
可以复用原登录态。只有明确选择「同时删除配置目录」时才连条目一起忘掉。
"""

from __future__ import annotations

import json
import random
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import paths

# 允许测试替换成临时路径（函数里每次都重新读这个模块级变量）
INDEX_FILE = paths.PROFILES_INDEX

INDEX_VERSION = 1

# 目录名白名单：本工具只会分配这两种形态，删除时用它兜底防误删
_FOLDER_RE = re.compile(r"^(?:p)?[0-9a-f]{12}$")

# 指纹种子的取值范围，与 cloakbrowser 内部的默认生成方式保持一致
# （config.py::get_default_stealth_args 用 random.randint(10000, 99999)）。
# 不自创格式，免得将来内核按范围校验时对不上。
FINGERPRINT_MIN = 10000
FINGERPRINT_MAX = 99999


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def random_folder_name() -> str:
    """分配一个随机的配置目录名（不与抖音号、用户名产生任何关联）。"""
    return "p" + uuid.uuid4().hex[:12]


def random_fingerprint() -> str:
    """分配一个随机的浏览器指纹种子（只在首次绑定时用一次，之后固定不变）。"""
    return str(random.randint(FINGERPRINT_MIN, FINGERPRINT_MAX))


def is_managed_folder(folder) -> bool:
    """目录名是否是本工具分配的形态 —— 删除操作的前置校验。"""
    return bool(_FOLDER_RE.match(str(folder or "")))


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def load_with_notes() -> tuple[dict, list]:
    """读取对照表，返回 (账号字典, 提示信息列表)。文件不存在就返回空表。"""
    notes: list = []
    path = Path(INDEX_FILE)
    if not path.is_file():
        return {}, notes

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        notes.append(f"{path.name} 读取失败（{type(exc).__name__}: {exc}），本次按空表处理")
        return {}, notes

    raw = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        notes.append(f"{path.name} 结构不对（应为 {{\"accounts\": {{...}}}}），已忽略")
        return {}, notes

    clean: dict = {}
    for unique_id, record in raw.items():
        if isinstance(record, dict) and record.get("folder"):
            clean[str(unique_id)] = dict(record)
    return clean, notes


def load() -> dict:
    return load_with_notes()[0]


def save(accounts: dict) -> None:
    """整表写回（先写临时文件再替换，避免中途崩掉留下半个文件）。"""
    path = Path(INDEX_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": INDEX_VERSION, "accounts": accounts}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def folder_for(accounts: dict, unique_id: str) -> str:
    """查这个抖音号已经绑定的配置目录名，没有就返回空串。"""
    record = accounts.get(str(unique_id or "").strip())
    if isinstance(record, dict):
        return str(record.get("folder") or "")
    return ""


def record_for(accounts: dict, unique_id: str) -> dict:
    record = accounts.get(str(unique_id or "").strip())
    return dict(record) if isinstance(record, dict) else {}


def fingerprint_for(accounts: dict, unique_id: str = "", folder: str = "") -> str:
    """查这个账号已经固定的指纹种子，没记录过就返回空串。

    先按抖音号查；查不到再按配置目录反查 —— 添加账号时抖音号要等浏览器
    抓回来才知道，而配置目录一开始就分配好了，那时只能靠目录认人。
    """
    unique_id = str(unique_id or "").strip()
    if unique_id:
        record = accounts.get(unique_id)
        if isinstance(record, dict):
            got = str(record.get("fingerprint") or "").strip()
            if got:
                return got

    folder = str(folder or "").strip()
    if folder:
        for record in accounts.values():
            if isinstance(record, dict) and str(record.get("folder") or "") == folder:
                got = str(record.get("fingerprint") or "").strip()
                if got:
                    return got
    return ""


def _ordered_record(record: dict, seed: str) -> dict:
    """把 fingerprint 排到 folder 后面。

    它和 folder 一样属于「绑定信息」，排在文件开头一眼就能找到；
    直接 append 到旧记录末尾会飘在 conversations 后面，很难看。
    """
    ordered = {"folder": str(record.get("folder") or ""), "fingerprint": seed}
    for key, value in record.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def ensure_fingerprint(unique_id: str = "", folder: str = "", fallback: str = "") -> str:
    """取这个账号的固定指纹；没有就分配一个并写回 profiles.json。

    写回哪一条：优先按抖音号找，其次按配置目录反查。
    都对不上时（例如刚点「添加账号」，还没登录、也没有记录）只返回新值，
    由调用方在登录成功后 ``bind()`` 时一并落盘 —— 否则会凭空造出一条
    没有目录的脏记录。

    ``fallback`` 用于「profiles.json 里查不到、但 .env 的 TASKS 里带着指纹」的
    情况（profiles.json 被删过、或者换了一台机器只用 .env）。这时优先信 .env 的
    值，否则每启动一次就换一个指纹，等于没固定。
    """
    unique_id = str(unique_id or "").strip()
    folder = str(folder or "").strip()
    fallback = str(fallback or "").strip()

    accounts = load()
    target_key = unique_id if unique_id in accounts else ""
    if not target_key and folder:
        for key, record in accounts.items():
            if isinstance(record, dict) and str(record.get("folder") or "") == folder:
                target_key = key
                break

    if not target_key:
        return (
            fingerprint_for(accounts, unique_id, folder)
            or fallback
            or random_fingerprint()
        )

    record = accounts[target_key]
    existing = str(record.get("fingerprint") or "").strip()
    if existing and list(record)[:2] == ["folder", "fingerprint"]:
        return existing  # 已经定好且顺序正常，不必白写一次文件

    seed = existing or random_fingerprint()
    accounts[target_key] = _ordered_record(record, seed)
    try:
        save(accounts)
    except Exception:
        # 写不进去也不该挡住这次启动，只是下次会再换一个（下次还有机会写成功）
        pass
    return seed


def conversations_for(accounts: dict, unique_id: str) -> list:
    """查这个抖音号上次抓到的会话名单（没有就返回空列表）。"""
    record = record_for(accounts, unique_id)
    names = record.get("conversations")
    if not isinstance(names, list):
        return []
    return [str(name) for name in names if str(name or "").strip()]


def conversations_at_for(accounts: dict, unique_id: str) -> str:
    return str(record_for(accounts, unique_id).get("conversations_at") or "")


def profile_dir(folder: str) -> Path:
    """配置目录的绝对路径。"""
    return paths.PROFILE_ROOT / str(folder)


def describe(folder: str) -> str:
    """给界面看的短路径（相对程序目录）。"""
    return f"profiles/{folder}" if folder else "（未分配）"


def allocated() -> list:
    """已经落在磁盘上的配置目录名（用于找出无用目录）。"""
    root = Path(paths.PROFILE_ROOT)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
def bind(
    unique_id: str,
    folder: str,
    *,
    nickname: str = "",
    uid: str = "",
    sec_uid: str = "",
    id_source: str = "",
    fingerprint: str = "",
) -> dict:
    """把某个抖音号绑定到配置目录并落盘，返回该条目。

    fingerprint 只在首次绑定时有意义：传空则沿用已有值，都没有就新分配一个 ——
    保证「每条记录都有固定指纹」这个不变量，省得每个调用点都自己补。
    """
    unique_id = str(unique_id or "").strip()
    folder = str(folder or "").strip()
    if not unique_id or not folder:
        raise ValueError("bind() 需要同时提供抖音号与配置目录名")

    accounts = load()
    existing = accounts.get(unique_id) or {}
    record = {
        "folder": folder,
        "fingerprint": str(
            fingerprint or existing.get("fingerprint") or random_fingerprint()
        ).strip(),
        "nickname": str(nickname or existing.get("nickname") or ""),
        "uid": str(uid or existing.get("uid") or ""),
        "sec_uid": str(sec_uid or existing.get("sec_uid") or ""),
        "id_source": str(id_source or existing.get("id_source") or ""),
        "created_at": str(existing.get("created_at") or _now()),
        "updated_at": _now(),
        # 之前的目录名留个痕迹，方便排查「换过目录」
        "previous_folder": existing.get("folder")
        if existing.get("folder") and existing.get("folder") != folder
        else str(existing.get("previous_folder") or ""),
    }
    accounts[unique_id] = record
    save(accounts)
    return record


def touch(unique_id: str, **fields) -> dict | None:
    """只更新条目里的展示字段（不改目录名）。条目不存在则返回 None。"""
    unique_id = str(unique_id or "").strip()
    accounts = load()
    record = accounts.get(unique_id)
    if not isinstance(record, dict):
        return None
    for key in ("nickname", "uid", "sec_uid", "id_source"):
        value = fields.get(key)
        if value:
            record[key] = str(value)
    record["updated_at"] = _now()
    save(accounts)
    return record


def set_conversations(unique_id: str, names, *, folder: str = "") -> dict:
    """保存某个账号抓到的会话名单（供界面直接选择目标好友）。

    只动 ``conversations`` / ``conversations_at`` 两个字段，**不碰 updated_at** ——
    界面上的「最近刷新」说的是登录态刷新时间，抓会话列表不该让它看起来像重新登录过。

    条目不存在时（极少见：账号还没走过登录流程）用传入的 folder 补一条，
    否则界面每次都会白抓一遍却存不下来。
    """
    unique_id = str(unique_id or "").strip()
    if not unique_id:
        raise ValueError("set_conversations() 需要一个抖音号")

    clean = [str(name).strip() for name in (names or []) if str(name or "").strip()]

    accounts = load()
    record = accounts.get(unique_id)
    if not isinstance(record, dict):
        folder = str(folder or "").strip()
        if not folder:
            raise ValueError(f"profiles.json 里没有 {unique_id} 的记录，也没给配置目录，无法保存")
        record = {
            "folder": folder,
            "fingerprint": random_fingerprint(),
            "nickname": "",
            "uid": "",
            "sec_uid": "",
            "id_source": "",
            "created_at": _now(),
            "updated_at": _now(),
            "previous_folder": "",
        }

    record["conversations"] = clean
    record["conversations_at"] = _now()
    accounts[unique_id] = record
    save(accounts)
    return dict(record)


def forget(unique_id: str) -> bool:
    """忘掉某个抖音号的绑定关系，返回是否真的删掉了条目。"""
    unique_id = str(unique_id or "").strip()
    accounts = load()
    if unique_id not in accounts:
        return False
    accounts.pop(unique_id)
    save(accounts)
    return True


def remove_profile_dir(folder: str) -> tuple[bool, str]:
    """删除一个配置目录。

    这是唯一一处会删磁盘内容的地方，所以设了三道闸：
      1. 目录名必须匹配本工具的命名规则（随机十六进制）
      2. 必须是 PROFILE_ROOT 的**直接**子目录（不允许任何路径跳转）
      3. 目标必须真实存在且是目录

    任何一条不满足都直接拒绝，返回 (是否成功, 说明)。
    """
    folder = str(folder or "").strip()
    if not is_managed_folder(folder):
        return False, f"目录名「{folder}」不像本工具分配的（应为 12 位十六进制），已拒绝删除"

    root = Path(paths.PROFILE_ROOT).resolve()
    try:
        target = (root / folder).resolve()
    except OSError as exc:
        return False, f"路径解析失败：{exc}"

    if target.parent != root:
        return False, f"目标不在配置目录下，已拒绝删除：{target}"
    if not target.exists():
        return True, "目录本来就不存在"
    if not target.is_dir():
        return False, f"目标不是目录，已拒绝删除：{target}"

    try:
        shutil.rmtree(target)
    except Exception as exc:
        return False, f"删除失败：{type(exc).__name__}: {exc}"
    return True, f"已删除 {target}"
