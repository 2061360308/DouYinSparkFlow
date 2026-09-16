"""配置数据模型与默认值。

默认值全部对齐 docs/static/js/main.js 的 form 初值，键顺序与 .env.example 一致。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 可选项
# ---------------------------------------------------------------------------
HITOKOTO_OPTIONS = [
    "动画",
    "漫画",
    "游戏",
    "文学",
    "原创",
    "来自网络",
    "影视",
    "诗词",
    "哲学",
    "抖机灵",
    "其他",
]

TZ_OPTIONS = ["Asia/Shanghai", "Asia/Hong_Kong", "Asia/Tokyo", "UTC"]

# utils/logger.py::resolve_log_level 会做 lower() 映射，所以这里保持页面的写法
LOG_LEVEL_OPTIONS = ["Debug", "Info", "Warning", "Error"]

# ---------------------------------------------------------------------------
# 默认值与取值范围
# ---------------------------------------------------------------------------
DEFAULT_PROXY_ADDRESS = ""
DEFAULT_RUN_TIME = "09:00:00"
DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_MESSAGE_TEMPLATE = "[盖瑞]今日火花[加一]\n—— [右边] 每日一言 [左边] ——\n[API]"
DEFAULT_HITOKOTO_TYPES = ["文学", "影视", "诗词", "哲学"]
DEFAULT_BROWSER_TIMEOUT = 120000
DEFAULT_FRIEND_LIST_WAIT_TIME = 2000
DEFAULT_TASK_RETRY_TIMES = 3
DEFAULT_LOG_LEVEL = "Info"

BROWSER_TIMEOUT_RANGE = (5000, 300000)
FRIEND_LIST_WAIT_RANGE = (1000, 120000)
RETRY_TIMES_RANGE = (1, 5)

# 写进 .env 的键顺序：先基础变量，再按账户顺序追加 COOKIES_*
BASE_ENV_KEYS = [
    "PROXY_ADDRESS",
    "CRON_HOUR",
    "CRON_MINUTE",
    "CRON_SECOND",
    "TZ",
    "MESSAGE_TEMPLATE",
    "HITOKOTO_TYPES",
    "BROWSER_TIMEOUT",
    "FRIEND_LIST_WAIT_TIME",
    "TASK_RETRY_TIMES",
    "LOG_LEVEL",
    "TASKS",
]

# unique_id 直接决定 COOKIES_<unique_id> 的键名，必须是环境变量安全的字符
SAFE_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def split_run_time(value: str) -> tuple[str, str, str]:
    """把 HH:MM:SS 拆成 (时, 分, 秒)，非法时回落到默认值。"""
    parts = (value or "").split(":")
    if len(parts) != 3:
        parts = DEFAULT_RUN_TIME.split(":")
    result = []
    for index, part in enumerate(parts):
        try:
            number = int(part.strip())
        except ValueError:
            number = int(DEFAULT_RUN_TIME.split(":")[index])
        result.append(max(0, min(59 if index else 23, number)))
    return (f"{result[0]:02d}", f"{result[1]:02d}", f"{result[2]:02d}")


def build_run_time(hour, minute, second) -> str:
    def clamp(raw, top):
        try:
            number = int(str(raw).strip())
        except (TypeError, ValueError):
            number = 0
        return max(0, min(top, number))

    return f"{clamp(hour, 23):02d}:{clamp(minute, 59):02d}:{clamp(second, 59):02d}"


# ---------------------------------------------------------------------------
# 账户
# ---------------------------------------------------------------------------
@dataclass
class Account:
    """一个抖音账号。

    所有身份字段（用户名 / 抖音号 / cookies）都由浏览器登录后自动抓取，
    界面上只读 —— 手填只会填错，而抖音号直接决定 .env 里的 COOKIES_ 键名。

    profile_folder  浏览器配置目录名。由 profile_store 分配并持久化到 profiles.json，
                    加载 .env 后可以凭 unique_id 找回来。空串表示还没分配过。
    fingerprint     浏览器指纹种子。profiles.json 里存一份（权威来源），固定不变，
                    打开浏览器时以 --fingerprint=<它> 传给隐身浏览器；生成 TASKS 时
                    也会一起写进 .env，让主程序能用同一个种子。空串表示还没有记录。
    cookies         单行 JSON 文本（就是写进 COOKIES_<unique_id> 的形态）
    targets         目标好友，只能从抓取到的会话列表里勾选
    """

    username: str = ""
    unique_id: str = ""
    cookies: str = ""
    targets: list = field(default_factory=list)
    profile_folder: str = ""
    fingerprint: str = ""

    @property
    def display_name(self) -> str:
        return self.username or self.unique_id or "（未登录账号）"

    @property
    def cookies_key(self) -> str:
        """对应的 .env 键名。config.py 里是 f"cookies_{unique_id}".upper()。"""
        return f"COOKIES_{self.unique_id.strip().upper()}"

    @property
    def cookie_count(self) -> int:
        text = (self.cookies or "").strip()
        if not text:
            return 0
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return 0
        return len(data) if isinstance(data, list) else 0

    @property
    def has_cookies(self) -> bool:
        return self.cookie_count > 0

    @property
    def cookie_status(self) -> str:
        if not (self.cookies or "").strip():
            return "待登录"
        return f"已获取 {self.cookie_count} 项" if self.cookie_count else "格式错误"

    def to_task(self) -> dict:
        """TASKS 数组里的一项。

        fingerprint 一并写进 .env：主程序拿它可以给同一个账号固定浏览器指纹，
        用的是与 configTool 打开浏览器时完全相同的种子（profiles.json 里那份
        仍是权威来源，这里只是把值带出去）。
        """
        return {
            "username": self.username,
            "unique_id": self.unique_id.strip(),
            "fingerprint": self.fingerprint.strip(),
            "targets": list(self.targets),
        }


# ---------------------------------------------------------------------------
# 整体配置
# ---------------------------------------------------------------------------
@dataclass
class Config:
    proxy_address: str = DEFAULT_PROXY_ADDRESS
    run_time: str = DEFAULT_RUN_TIME
    tz: str = DEFAULT_TZ
    # 内存里保存真实换行，写盘时才转成字面 \n
    message_template: str = DEFAULT_MESSAGE_TEMPLATE
    hitokoto_types: list = field(default_factory=lambda: list(DEFAULT_HITOKOTO_TYPES))
    browser_timeout: int = DEFAULT_BROWSER_TIMEOUT
    friend_list_wait_time: int = DEFAULT_FRIEND_LIST_WAIT_TIME
    task_retry_times: int = DEFAULT_TASK_RETRY_TIMES
    log_level: str = DEFAULT_LOG_LEVEL
    accounts: list = field(default_factory=list)

    # -- 序列化 -------------------------------------------------------------
    def to_env_map(self) -> dict:
        """生成 键 -> 值 的映射，值已经是能直接写进 .env 的形态。"""
        hour, minute, second = split_run_time(self.run_time)
        template = (self.message_template or "").replace("\r", "").replace("\n", "\\n")

        env = {
            "PROXY_ADDRESS": self.proxy_address or "",
            "CRON_HOUR": hour,
            "CRON_MINUTE": minute,
            "CRON_SECOND": second,
            "TZ": self.tz or DEFAULT_TZ,
            "MESSAGE_TEMPLATE": template,
            "HITOKOTO_TYPES": json.dumps(
                self.hitokoto_types or [], ensure_ascii=False, separators=(",", ":")
            ),
            "BROWSER_TIMEOUT": str(int(self.browser_timeout)),
            "FRIEND_LIST_WAIT_TIME": str(int(self.friend_list_wait_time)),
            "TASK_RETRY_TIMES": str(int(self.task_retry_times)),
            "LOG_LEVEL": self.log_level or DEFAULT_LOG_LEVEL,
            # TASKS 不走 unicode_escape，保持中文可读（与 index.html / .env.example 一致）
            "TASKS": json.dumps(
                [account.to_task() for account in self.accounts],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }

        for account in self.accounts:
            # 抖音号为空时没有合法键名，跳过（校验环节会报错提醒）
            if account.unique_id.strip():
                env[account.cookies_key] = (account.cookies or "").strip()

        return env

    def render_env_text(self) -> str:
        """「复制 .env 内容」用的文本，逐行 KEY=VALUE。"""
        return "\n".join(f"{key}={value}" for key, value in self.to_env_map().items())

    # -- 反序列化 -----------------------------------------------------------
    @classmethod
    def from_env_map(cls, mapping: dict) -> "Config":
        def text(key: str, default: str = "") -> str:
            value = mapping.get(key)
            return default if value is None else str(value)

        def number(key: str, default: int, low: int, high: int) -> int:
            try:
                value = int(float(text(key, str(default)).strip() or default))
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        accounts: list = []
        try:
            raw_tasks = json.loads(text("TASKS", "[]") or "[]")
        except json.JSONDecodeError:
            raw_tasks = []
        if not isinstance(raw_tasks, list):
            raw_tasks = []

        for task in raw_tasks:
            if not isinstance(task, dict):
                continue
            unique_id = str(task.get("unique_id", "") or "").strip()
            cookies_key = f"COOKIES_{unique_id.upper()}"
            raw_targets = task.get("targets") or []
            if not isinstance(raw_targets, list):
                raw_targets = [raw_targets]
            accounts.append(
                Account(
                    username=str(task.get("username", "") or ""),
                    unique_id=unique_id,
                    cookies=text(cookies_key).strip(),
                    targets=[str(t) for t in raw_targets if str(t).strip()],
                    # 老版本的 TASKS 里没有这个字段，取不到就留空
                    fingerprint=str(task.get("fingerprint", "") or "").strip(),
                )
            )

        run_time = build_run_time(
            text("CRON_HOUR", split_run_time(DEFAULT_RUN_TIME)[0]),
            text("CRON_MINUTE", split_run_time(DEFAULT_RUN_TIME)[1]),
            text("CRON_SECOND", split_run_time(DEFAULT_RUN_TIME)[2]),
        )

        try:
            hitokoto = json.loads(text("HITOKOTO_TYPES", "[]") or "[]")
        except json.JSONDecodeError:
            hitokoto = list(DEFAULT_HITOKOTO_TYPES)
        if not isinstance(hitokoto, list) or not hitokoto:
            hitokoto = list(DEFAULT_HITOKOTO_TYPES)

        return cls(
            proxy_address=text("PROXY_ADDRESS"),
            run_time=run_time,
            tz=text("TZ", DEFAULT_TZ) or DEFAULT_TZ,
            # 磁盘上是字面 \n，换回真实换行方便在文本框里编辑
            message_template=text("MESSAGE_TEMPLATE", DEFAULT_MESSAGE_TEMPLATE).replace(
                "\\n", "\n"
            ),
            hitokoto_types=[str(item) for item in hitokoto],
            browser_timeout=number("BROWSER_TIMEOUT", DEFAULT_BROWSER_TIMEOUT, *BROWSER_TIMEOUT_RANGE),
            friend_list_wait_time=number(
                "FRIEND_LIST_WAIT_TIME", DEFAULT_FRIEND_LIST_WAIT_TIME, *FRIEND_LIST_WAIT_RANGE
            ),
            task_retry_times=number(
                "TASK_RETRY_TIMES", DEFAULT_TASK_RETRY_TIMES, *RETRY_TIMES_RANGE
            ),
            log_level=text("LOG_LEVEL", DEFAULT_LOG_LEVEL) or DEFAULT_LOG_LEVEL,
            accounts=accounts,
        )


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def validate(config: Config) -> list:
    """返回 [(级别, 说明)]，级别为 '错误' 或 '警告'。

    目标是提前拦住那些「只在运行时才暴露」的问题，例如 config.py 里
    「缺少 COOKIES_xxx 环境变量，已跳过」这种静默跳过的坑。
    """
    issues: list = []

    hour, minute, second = split_run_time(config.run_time)
    if f"{hour}:{minute}:{second}" != (config.run_time or "").strip():
        issues.append(("错误", f"执行时间格式应为 HH:MM:SS，当前是「{config.run_time}」"))
    if not config.message_template.strip():
        issues.append(("错误", "消息模板不能为空"))
    if not config.hitokoto_types:
        issues.append(("警告", "一言类型一个都没勾选，[API] 可能拿不到内容"))
    if not config.accounts:
        issues.append(("错误", "至少要有一个账户"))

    seen: dict = {}
    for account in config.accounts:
        name = account.username.strip() or "（未命名账户）"
        unique_id = account.unique_id.strip()

        if not name or name == "（未命名账户）":
            issues.append(("警告", f"{name}：用户名只是日志标识，建议填一个"))
        if not unique_id:
            issues.append(("错误", f"{name}：抖音号必填，它决定 .env 里的 COOKIES_ 键名"))
        else:
            bad = sorted({ch for ch in unique_id if ch not in SAFE_ID_CHARS})
            if bad:
                issues.append(
                    (
                        "错误",
                        f"{name}：抖音号含非法字符 {' '.join(bad)}，会导致 {account.cookies_key} 不是合法的环境变量名",
                    )
                )
            key = account.cookies_key
            if key in seen:
                issues.append(
                    ("错误", f"{name}：抖音号与「{seen[key]}」重复，{key} 会互相覆盖")
                )
            else:
                seen[key] = name

        if not (account.cookies or "").strip():
            issues.append(
                ("错误", f"{name}：还没有 Cookies，点「刷新登录信息」会自动打开浏览器抓取")
            )
        else:
            try:
                data = json.loads(account.cookies)
            except json.JSONDecodeError as exc:
                issues.append(("错误", f"{name}：Cookies 不是合法 JSON（{exc.msg}）"))
            else:
                if not isinstance(data, list):
                    issues.append(("错误", f"{name}：Cookies 必须是 JSON 数组"))
                elif not data:
                    issues.append(("错误", f"{name}：Cookies 数组是空的"))

        if not account.targets:
            issues.append(("错误", f"{name}：至少要填一个目标好友"))

        if not account.fingerprint.strip():
            # 不拦着保存：没有配置目录的新账号本来也分不到指纹。
            # 但 TASKS 里的 fingerprint 会是空串，主程序若依赖它就会每次换指纹。
            issues.append(
                ("警告", f"{name}：还没有浏览器指纹，TASKS 里的 fingerprint 会是空的")
            )

    return issues
