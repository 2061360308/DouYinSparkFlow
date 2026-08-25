# Douyin Spark Multiuser Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy an isolated, resource-bounded multiuser web console where invited users manage their own Douyin spark tasks and an administrator manages users, schedules, and redacted run status.

**Architecture:** Add a `spark_console` FastAPI package beside the existing automation code. A server-rendered web container and a single-concurrency worker container share one SQLite WAL database and one application image; the worker adapts the existing asynchronous Playwright flow and owns all Cookie decryption. The new Compose project binds only to loopback, uses its own network and volume, and leaves every BPS container and resource untouched.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, SQLAlchemy 2, SQLite WAL, Argon2id, AES-256-GCM, Playwright 1.56, unittest, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-08-25-douyin-spark-multiuser-design.md`

## Global Constraints

- Preserve all existing BPS containers, images, volumes, networks, ports `8888/8443`, and Nginx configuration.
- Never log, return, commit, or expose Cookie, password, Token, secret, or environment-variable values.
- Bind the new web service to `127.0.0.1:8899` until a user-supplied domain and trusted HTTPS endpoint are approved.
- Store timestamps in UTC and display schedules in `Asia/Shanghai`; refuse task execution while measured system clock offset exceeds 5 seconds.
- Run at most one Chromium task globally and never automatically retry a failed send.
- Use test-first development for every production behavior and keep the existing unittest suite green.
- Do not disable the existing WZ/GSY systemd timers until imported accounts pass no-send validation and a maintenance window is explicitly approved.
- Stop deployment if root filesystem free space is below 5 GiB or if any BPS preflight health check changes.
- Push only the feature branch `feat/multiuser-console`; do not update `main`.

---

## File Structure

```text
spark_console/
  __init__.py              application package
  config.py                validated paths, secrets, limits, and timezone
  db.py                    SQLite engine, WAL pragmas, sessions, schema bootstrap
  models.py                users, accounts, tasks, runs, sessions, locks, audits
  crypto.py                AES-256-GCM Cookie encryption boundary
  security.py              Argon2id passwords, session tokens, CSRF
  scheduler.py             next-run calculation, leases, idempotent task claims
  executor.py              Playwright adapter and redacted execution stages
  worker.py                single-concurrency polling process
  services/
    accounts.py            owner-scoped encrypted account operations
    tasks.py               owner-scoped task CRUD and validation
    users.py               admin user lifecycle and password resets
    audits.py              secret-free audit writer
  web/
    app.py                 FastAPI application factory
    dependencies.py        session, RBAC, CSRF, and owner-scope dependencies
    auth.py                login, logout, and first-password-change routes
    user_routes.py         user dashboard, accounts, tasks, and runs
    admin_routes.py        user/task administration and deletion routes
  templates/               server-rendered accessible HTML
  static/app.css           porcelain/ink/ember visual system
  cli.py                   admin creation, legacy import, backup, safe probes
tests/console/              isolated unit and web integration tests
Dockerfile.console          shared Playwright application image
compose.console.yml         isolated web/worker Compose project
.env.console.example        non-secret variable names and safe defaults
docs/console-operations.md  build, deploy, migrate, verify, rollback
```

---

### Task 1: Console dependencies, configuration, and SQLite schema

**Files:**
- Create: `requirements-console.txt`
- Create: `spark_console/__init__.py`
- Create: `spark_console/config.py`
- Create: `spark_console/db.py`
- Create: `spark_console/models.py`
- Create: `tests/console/__init__.py`
- Create: `tests/console/test_config_db.py`

**Interfaces:**
- Produces: `Settings.from_env(environ) -> Settings`, `create_engine_for(settings) -> Engine`, `session_scope(engine)`, `create_schema(engine)`, and SQLAlchemy models used by every later task.
- Consumes: no earlier task interfaces.

- [ ] **Step 1: Add failing configuration and database tests**

```python
class SettingsTests(unittest.TestCase):
    def test_requires_existing_32_byte_cookie_key_file(self):
        with tempfile.TemporaryDirectory() as root:
            key = Path(root, "cookie.key")
            key.write_bytes(b"short")
            with self.assertRaisesRegex(ValueError, "exactly 32 bytes"):
                Settings.from_env({
                    "SPARK_DATA_DIR": root,
                    "SPARK_COOKIE_KEY_FILE": str(key),
                    "SPARK_SESSION_KEY_FILE": str(Path(root, "session.key")),
                })

class DatabaseTests(unittest.TestCase):
    def test_schema_enforces_unique_enabled_schedule(self):
        engine = make_test_engine()
        create_schema(engine)
        with session_scope(engine) as session:
            user = User(username="friend", password_hash="hash", role="user")
            session.add(user)
            session.flush()
            account = DouyinAccount(owner_user_id=user.id, display_name="main")
            session.add(account)
            session.flush()
            session.add(SparkTask(owner_user_id=user.id, douyin_account_id=account.id,
                                  target_name="目标", send_time="09:00", enabled=True))
            session.flush()
            session.add(SparkTask(owner_user_id=user.id, douyin_account_id=account.id,
                                  target_name="目标", send_time="09:00", enabled=True))
            with self.assertRaises(IntegrityError):
                session.flush()
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m unittest -v tests.console.test_config_db`
Expected: import failure because `spark_console.config`, `db`, and `models` do not exist.

- [ ] **Step 3: Add pinned console dependencies**

```text
fastapi==0.116.1
uvicorn==0.35.0
Jinja2==3.1.6
python-multipart==0.0.20
SQLAlchemy==2.0.43
argon2-cffi==25.1.0
cryptography==45.0.6
httpx==0.28.1
```

- [ ] **Step 4: Implement settings and schema**

```python
@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    cookie_key_file: Path
    session_key_file: Path
    timezone: str = "Asia/Shanghai"
    web_bind: str = "127.0.0.1"
    web_port: int = 8899
    worker_poll_seconds: int = 10
    clock_offset_limit_seconds: int = 5

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Settings":
        data_dir = Path(environ["SPARK_DATA_DIR"]).resolve()
        cookie_key = Path(environ["SPARK_COOKIE_KEY_FILE"]).resolve()
        session_key = Path(environ["SPARK_SESSION_KEY_FILE"]).resolve()
        if len(cookie_key.read_bytes()) != 32:
            raise ValueError("cookie key must be exactly 32 bytes")
        if len(session_key.read_bytes()) < 32:
            raise ValueError("session key must contain at least 32 bytes")
        return cls(data_dir, f"sqlite:///{data_dir / 'spark.db'}", cookie_key, session_key)
```

Implement UUID primary keys, UTC timestamps, the six specified tables, foreign keys, indexes, WAL, `busy_timeout=5000`, and the partial unique index for enabled duplicate tasks.

- [ ] **Step 5: Run focused and full tests and verify GREEN**

Run: `python -m unittest -v tests.console.test_config_db && python -m unittest discover -s tests -v`
Expected: all tests pass with no warnings containing credential values.

- [ ] **Step 6: Commit the foundation**

```bash
git add requirements-console.txt spark_console tests/console
git commit -m "feat: add console configuration and database schema"
```

---

### Task 2: Cookie encryption, passwords, sessions, and CSRF

**Files:**
- Create: `spark_console/crypto.py`
- Create: `spark_console/security.py`
- Create: `tests/console/test_crypto_security.py`

**Interfaces:**
- Consumes: `Settings`, `User`, `WebSession`, and `session_scope` from Task 1.
- Produces: `CookieCipher.encrypt(payload) -> EncryptedPayload`, `CookieCipher.decrypt(ciphertext, nonce) -> bytes`, `PasswordService`, `SessionService`, and `CsrfService`.

- [ ] **Step 1: Write failing encryption and authentication tests**

```python
class CookieCipherTests(unittest.TestCase):
    def test_round_trip_and_tamper_detection(self):
        cipher = CookieCipher(b"k" * 32)
        sealed = cipher.encrypt(b'[{"name":"sessionid","value":"secret"}]')
        self.assertEqual(b'[{"name":"sessionid","value":"secret"}]',
                         cipher.decrypt(sealed.ciphertext, sealed.nonce))
        tampered = sealed.ciphertext[:-1] + bytes([sealed.ciphertext[-1] ^ 1])
        with self.assertRaises(InvalidTag):
            cipher.decrypt(tampered, sealed.nonce)

class SessionTests(unittest.TestCase):
    def test_database_stores_only_session_token_hash(self):
        raw, record = service.create_session(user_id)
        self.assertNotEqual(raw, record.token_hash)
        self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(), record.token_hash)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest -v tests.console.test_crypto_security`
Expected: import failure for `CookieCipher` and `SessionService`.

- [ ] **Step 3: Implement AES-256-GCM and security services**

```python
@dataclass(frozen=True)
class EncryptedPayload:
    ciphertext: bytes
    nonce: bytes

class CookieCipher:
    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("AES-256-GCM requires a 32-byte key")
        self._aes = AESGCM(key)

    def encrypt(self, payload: bytes) -> EncryptedPayload:
        nonce = os.urandom(12)
        return EncryptedPayload(self._aes.encrypt(nonce, payload, b"douyin-cookie-v1"), nonce)

    def decrypt(self, ciphertext: bytes, nonce: bytes) -> bytes:
        return self._aes.decrypt(nonce, ciphertext, b"douyin-cookie-v1")
```

Use Argon2id defaults from `argon2.PasswordHasher`, 32-byte URL-safe session tokens, SHA-256 token hashes, 8-hour expiry, 5-failure/15-minute lockout, `secrets.compare_digest`, and per-session CSRF tokens.

- [ ] **Step 4: Run focused and full tests and verify GREEN**

Run: `python -m unittest -v tests.console.test_crypto_security && python -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 5: Commit security boundaries**

```bash
git add spark_console/crypto.py spark_console/security.py tests/console/test_crypto_security.py
git commit -m "feat: secure console credentials and sessions"
```

---

### Task 3: Owner-scoped user, account, task, and audit services

**Files:**
- Create: `spark_console/services/__init__.py`
- Create: `spark_console/services/accounts.py`
- Create: `spark_console/services/tasks.py`
- Create: `spark_console/services/users.py`
- Create: `spark_console/services/audits.py`
- Create: `tests/console/test_services.py`

**Interfaces:**
- Consumes: models, `CookieCipher`, `PasswordService`, and database sessions.
- Produces: `AccountService`, `TaskService`, `UserService`, `AuditService`, and typed `NotFound`, `Conflict`, and `ValidationError` exceptions.

- [ ] **Step 1: Write failing owner-isolation and deletion tests**

```python
def test_user_cannot_read_or_delete_another_users_task(self):
    task = self.tasks.create(self.owner.id, self.owner_account.id,
                             "朋友", "09:00", "今日火花")
    with self.assertRaises(NotFound):
        self.tasks.get_owned(self.other.id, task.id)
    with self.assertRaises(NotFound):
        self.tasks.delete_owned(self.other.id, task.id)

def test_deleting_account_erases_cookie_and_disables_tasks(self):
    self.accounts.delete_owned(self.owner.id, self.owner_account.id)
    self.assertIsNone(self.session.get(DouyinAccount, self.owner_account.id))
    self.assertFalse(self.session.get(SparkTask, self.task.id).enabled)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest -v tests.console.test_services`
Expected: import failure for the service classes.

- [ ] **Step 3: Implement strict service boundaries**

```python
class TaskService:
    def get_owned(self, owner_id: UUID, task_id: UUID) -> SparkTask:
        task = self.session.scalar(select(SparkTask).where(
            SparkTask.id == task_id,
            SparkTask.owner_user_id == owner_id,
        ))
        if task is None:
            raise NotFound("task not found")
        return task

    def create(self, owner_id, account_id, target_name, send_time, message_template):
        validate_target(target_name, max_length=64)
        validate_hhmm(send_time)
        validate_message(message_template, max_length=500)
        account = self.accounts.get_owned(owner_id, account_id)
        task = SparkTask(owner_user_id=owner_id, douyin_account_id=account.id,
                         target_name=target_name.strip(), send_time=send_time,
                         message_template=message_template, enabled=True)
        self.session.add(task)
        self.audit.write(owner_id, "task.created", "spark_task", task.id)
        return task
```

Never expose `encrypted_cookies` or `cookie_nonce` from a service response. Implement user disable, password reset, task enable/disable/delete, account delete, and secret-free audit records exactly as specified.

- [ ] **Step 4: Run focused and full tests and verify GREEN**

Run: `python -m unittest -v tests.console.test_services && python -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 5: Commit service layer**

```bash
git add spark_console/services tests/console/test_services.py
git commit -m "feat: add owner-scoped console services"
```

---

### Task 4: Authentication, user dashboard, and visual system

**Files:**
- Create: `spark_console/web/__init__.py`
- Create: `spark_console/web/app.py`
- Create: `spark_console/web/dependencies.py`
- Create: `spark_console/web/auth.py`
- Create: `spark_console/web/user_routes.py`
- Create: `spark_console/templates/base.html`
- Create: `spark_console/templates/login.html`
- Create: `spark_console/templates/change_password.html`
- Create: `spark_console/templates/dashboard.html`
- Create: `spark_console/templates/accounts.html`
- Create: `spark_console/templates/tasks.html`
- Create: `spark_console/templates/runs.html`
- Create: `spark_console/static/app.css`
- Create: `tests/console/test_web_user.py`

**Interfaces:**
- Consumes: Task 2 security and Task 3 owner-scoped services.
- Produces: `create_app(settings, engine) -> FastAPI`, authenticated HTML routes, and `/health/live` plus `/health/ready`.

- [ ] **Step 1: Write failing login, forced-password-change, CSRF, and isolation tests**

```python
def test_first_login_redirects_only_to_password_change(self):
    response = self.client.post("/login", data={"username": "friend", "password": "Temp-1234"}, follow_redirects=False)
    self.assertEqual(303, response.status_code)
    self.assertEqual("/change-password", response.headers["location"])
    dashboard = self.client.get("/dashboard", follow_redirects=False)
    self.assertEqual("/change-password", dashboard.headers["location"])

def test_post_without_csrf_is_rejected(self):
    response = self.client.post("/tasks", data={"target_name": "朋友"})
    self.assertEqual(403, response.status_code)

def test_cookie_value_never_appears_after_account_save(self):
    response = self.client.post("/accounts", data=valid_account_form(self.csrf))
    self.assertNotIn("sessionid-secret", response.text)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest -v tests.console.test_web_user`
Expected: import failure for `create_app`.

- [ ] **Step 3: Implement FastAPI application and routes**

```python
def create_app(settings: Settings, engine: Engine) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.engine = engine
    app.mount("/static", StaticFiles(directory="spark_console/static"), name="static")
    app.include_router(auth.router)
    app.include_router(user_routes.router)
    return app
```

Use `303` after successful form posts, owner-scoped service calls for every resource, generic 404 for cross-owner IDs, secure session Cookies, CSRF hidden fields, and plain-language error messages.

- [ ] **Step 4: Implement the approved visual system**

```css
:root {
  --porcelain: #f7f8fc;
  --ink: #182033;
  --ember: #ff6b57;
  --mist: #e8ecf5;
  --jade: #1e9b72;
  --amber: #d98e24;
  --radius: 12px;
  --focus: 0 0 0 3px rgb(255 107 87 / 25%);
}
body { margin: 0; background: var(--porcelain); color: var(--ink); font-family: "PingFang SC", "Microsoft YaHei", system-ui, sans-serif; }
:focus-visible { outline: 0; box-shadow: var(--focus); }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; } }
```

Build responsive sidebar/bottom navigation, task forms, status pills with text and icon, empty states with direct actions, and the five-stage fire-track component. Do not add gradients, glass effects, external font requests, or client-side frameworks.

- [ ] **Step 5: Run focused and full tests and verify GREEN**

Run: `python -m unittest -v tests.console.test_web_user && python -m unittest discover -s tests -v`
Expected: all tests pass; rendered HTML contains no Cookie values.

- [ ] **Step 6: Commit user console**

```bash
git add spark_console/web spark_console/templates spark_console/static tests/console/test_web_user.py
git commit -m "feat: add secure user task console"
```

---

### Task 5: Search-first Playwright execution and explicit outcomes

**Files:**
- Modify: `core/web_chat.py`
- Modify: `core/tasks.py`
- Create: `spark_console/executor.py`
- Modify: `tests/test_web_chat.py`
- Modify: `tests/test_tasks.py`
- Create: `tests/console/test_executor.py`

**Interfaces:**
- Consumes: existing `confirm_message_sent`, Playwright browser helpers, and encrypted account/task records.
- Produces: `search_web_chat_target(page, target, timeout) -> bool`, `select_web_chat_target`, `ExecutionStage`, `ExecutionResult`, and `DouyinExecutor.execute(cookies, target, message) -> ExecutionResult`.

- [ ] **Step 1: Add failing tests for search-first selection and missing-target failure**

```python
async def test_search_panel_is_used_before_recent_conversation_list(self):
    page = FakeSearchPage(search_results=["ʚ繁花ɞ🌸"], conversations=[])
    selected = await select_web_chat_target(page, "ʚ繁花ɞ🌸", timeout=2000)
    self.assertEqual("ʚ繁花ɞ🌸", selected)
    self.assertTrue(page.search_results[0].clicked)
    self.assertFalse(page.conversation_list_requested)

async def test_missing_target_raises_and_cannot_report_success(self):
    page = FakeSearchPage(search_results=[], conversations=["其他好友"])
    with self.assertRaises(TargetNotFoundError):
        await select_web_chat_target(page, "ʚ繁花ɞ🌸", timeout=2000)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest -v tests.test_web_chat tests.console.test_executor`
Expected: search behavior or executor imports fail.

- [ ] **Step 3: Implement search-first selection with scroll fallback**

```python
SEARCH_INPUT_SELECTOR = 'input[placeholder="搜索"]'
SEARCH_RESULT_SELECTOR = ".SearchPanelitemsearch_highlight"

async def search_web_chat_target(page, target: str, timeout: int) -> bool:
    search = page.locator(SEARCH_INPUT_SELECTOR)
    if await search.count() == 0:
        return False
    await search.fill(target)
    await page.wait_for_timeout(min(timeout, 2000))
    for result in await page.locator(SEARCH_RESULT_SELECTOR).all():
        if (await result.inner_text()).strip() == target.strip():
            await result.click()
            try:
                await search.fill("")
            except Exception:
                pass
            return True
    await search.fill("")
    return False
```

If search returns false, call the existing exact conversation scan. If both fail, raise `TargetNotFoundError`. Rename shadowed loop variables from `username` to `target`, propagate all execution failures, and keep delivery confirmation.

- [ ] **Step 4: Implement the executor boundary**

```python
class ExecutionStage(StrEnum):
    BROWSER_START = "browser_start"
    LOGIN = "login"
    TARGET_SEARCH = "target_search"
    COMPOSE = "compose"
    SUBMIT = "submit"
    FINISHED = "finished"

@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    stage: ExecutionStage
    error_code: str | None = None
    error_summary: str | None = None

class DouyinExecutor:
    def __init__(self, browser_factory):
        self.browser_factory = browser_factory

    async def execute(self, cookies: list[dict], target: str, message: str) -> ExecutionResult:
        stage = ExecutionStage.BROWSER_START
        playwright = browser = context = None
        try:
            playwright, browser = await self.browser_factory()
            context = await browser.new_context()
            await context.add_cookies(cookies)
            page = await context.new_page()
            stage = ExecutionStage.LOGIN
            await page.goto(WEB_CHAT_URL)
            if "/chat" not in page.url:
                return ExecutionResult(False, stage, "cookie_expired", "抖音登录已失效")
            stage = ExecutionStage.TARGET_SEARCH
            await select_web_chat_target(page, target)
            stage = ExecutionStage.COMPOSE
            editor = page.locator(CHAT_EDITOR_SELECTOR).first
            await editor.type(message)
            stage = ExecutionStage.SUBMIT
            await editor.press("Enter")
            await confirm_message_sent(page, editor, message)
            return ExecutionResult(True, ExecutionStage.FINISHED)
        except TargetNotFoundError:
            return ExecutionResult(False, stage, "target_not_found", "未找到目标好友")
        except Exception as error:
            return map_execution_error(stage, error)
        finally:
            if context is not None:
                await context.close()
            if browser is not None:
                await browser.close()
            if playwright is not None:
                await playwright.stop()
```

Replace the ellipsis during implementation with the complete existing Playwright calls. Map `LoginRequiredError`, `TargetNotFoundError`, browser startup errors, and confirmation errors to the exact public error codes from the spec; log only stage and error code.

- [ ] **Step 5: Run focused and full tests and verify GREEN**

Run: `python -m unittest -v tests.test_web_chat tests.test_tasks tests.console.test_executor && python -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 6: Commit execution behavior**

```bash
git add core/web_chat.py core/tasks.py spark_console/executor.py tests/test_web_chat.py tests/test_tasks.py tests/console/test_executor.py
git commit -m "fix: make spark execution search-first and explicit"
```

---

### Task 6: Idempotent scheduler and single-concurrency worker

**Files:**
- Create: `spark_console/scheduler.py`
- Create: `spark_console/worker.py`
- Create: `tests/console/test_scheduler_worker.py`

**Interfaces:**
- Consumes: models, database sessions, `CookieCipher`, `DouyinExecutor`, and Task 3 services.
- Produces: `compute_next_run`, `claim_next_due_task`, `finish_run`, `Worker.run_once(now)`, and `python -m spark_console.worker`.

- [ ] **Step 1: Write failing timezone, missed-run, idempotency, and lease tests**

```python
def test_compute_next_run_uses_shanghai_timezone(self):
    now = datetime(2026, 8, 25, 0, 30, tzinfo=timezone.utc)
    self.assertEqual(datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc),
                     compute_next_run("09:00", now))

def test_same_scheduled_run_can_only_be_claimed_once(self):
    first = claim_next_due_task(self.session, self.now, worker_id="one")
    second = claim_next_due_task(self.session, self.now, worker_id="two")
    self.assertIsNotNone(first)
    self.assertIsNone(second)

def test_run_more_than_ten_minutes_late_is_skipped(self):
    result = self.worker.run_once(self.scheduled_for + timedelta(minutes=11))
    self.assertEqual("skipped", result.status)
    self.executor.assert_not_called()
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest -v tests.console.test_scheduler_worker`
Expected: import failure for scheduler and worker symbols.

- [ ] **Step 3: Implement UTC scheduling and transactional claims**

```python
def compute_next_run(send_time: str, now_utc: datetime) -> datetime:
    hour, minute = map(int, send_time.split(":"))
    zone = ZoneInfo("Asia/Shanghai")
    local_now = now_utc.astimezone(zone)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)
```

Use `BEGIN IMMEDIATE`, a single `worker_lock` row with a 20-minute lease, the `(task_id, scheduled_for)` unique index, and one `TaskRun` per claim. Older-than-10-minute tasks become `skipped`; failed tasks are never requeued automatically.

- [ ] **Step 4: Implement worker execution and cleanup**

```python
class Worker:
    async def run_once(self, now: datetime) -> TaskRun | None:
        claim = self.scheduler.claim(now, self.worker_id)
        if claim is None:
            return None
        if abs(self.clock.offset_seconds()) > self.settings.clock_offset_limit_seconds:
            return self.scheduler.fail(claim, "system_time_unhealthy", "服务器时间未同步")
        cookies = self.accounts.decrypt_for_worker(claim.account_id)
        try:
            result = await self.executor.execute(cookies, claim.target_name, claim.message_template)
            return self.scheduler.finish(claim, result)
        finally:
            cookies.clear()
```

The process loop sleeps using an interruptible event, handles SIGTERM, releases expired leases safely, and never logs decrypted data.

- [ ] **Step 5: Run focused and full tests and verify GREEN**

Run: `python -m unittest -v tests.console.test_scheduler_worker && python -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 6: Commit scheduler and worker**

```bash
git add spark_console/scheduler.py spark_console/worker.py tests/console/test_scheduler_worker.py
git commit -m "feat: add idempotent serial spark worker"
```

---

### Task 7: Administrator console and destructive-action safeguards

**Files:**
- Create: `spark_console/web/admin_routes.py`
- Create: `spark_console/templates/admin_users.html`
- Create: `spark_console/templates/admin_tasks.html`
- Create: `spark_console/templates/admin_runs.html`
- Create: `spark_console/templates/confirm_delete.html`
- Create: `tests/console/test_web_admin.py`

**Interfaces:**
- Consumes: admin RBAC dependency, user/task/account services, audit service, and session elevation from earlier tasks.
- Produces: admin-only user creation, password reset, unlock, disable, task enable/disable/delete, account/user delete, and system overview routes.

- [ ] **Step 1: Write failing admin authorization and confirmation tests**

```python
def test_normal_user_cannot_open_admin_tasks(self):
    response = self.user_client.get("/admin/tasks")
    self.assertEqual(404, response.status_code)

def test_delete_requires_recent_password_elevation_and_name_confirmation(self):
    response = self.admin_client.post(
        f"/admin/users/{self.friend.id}/delete",
        data={"csrf_token": self.csrf, "confirmation": "wrong"},
    )
    self.assertEqual(400, response.status_code)
    self.assertIsNotNone(self.session.get(User, self.friend.id))
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest -v tests.console.test_web_admin`
Expected: admin routes are missing.

- [ ] **Step 3: Implement admin routes and views**

```python
@router.post("/admin/tasks/{task_id}/disable")
def disable_task(task_id: UUID, admin=Depends(require_admin), csrf=Depends(require_csrf)):
    task_service.disable_admin(admin.id, task_id)
    return RedirectResponse("/admin/tasks", status_code=303)

@router.post("/admin/users/{user_id}/delete")
def delete_user(user_id: UUID, confirmation: str = Form(),
                admin=Depends(require_elevated_admin), csrf=Depends(require_csrf)):
    user_service.delete_admin(admin.id, user_id, confirmation)
    return RedirectResponse("/admin/users", status_code=303)
```

Render queue state, next run, last success, failure stage, and actionable redacted error. Never render password hashes, session hashes, Cookie fields, nonces, request bodies, or environment details.

- [ ] **Step 4: Run focused and full tests and verify GREEN**

Run: `python -m unittest -v tests.console.test_web_admin && python -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 5: Commit administrator console**

```bash
git add spark_console/web/admin_routes.py spark_console/templates/admin_*.html spark_console/templates/confirm_delete.html tests/console/test_web_admin.py
git commit -m "feat: add guarded administrator console"
```

---

### Task 8: CLI, legacy import, backup, and no-send validation

**Files:**
- Create: `spark_console/cli.py`
- Create: `tests/console/test_cli.py`
- Create: `docs/console-operations.md`

**Interfaces:**
- Consumes: settings, schema, services, cipher, and executor.
- Produces: `create-admin`, `import-legacy`, `backup-db`, and `verify-account --no-send` commands.

- [ ] **Step 1: Write failing CLI tests**

```python
def test_import_legacy_encrypts_cookie_and_never_prints_it(self):
    result = run_cli(["import-legacy", "--config", str(self.legacy_json)])
    self.assertEqual(0, result.exit_code)
    self.assertNotIn("session-secret", result.stdout + result.stderr)
    account = self.session.scalar(select(DouyinAccount))
    self.assertNotIn(b"session-secret", account.encrypted_cookies)

def test_backup_uses_timestamped_private_file(self):
    result = run_cli(["backup-db"])
    backup = Path(result.stdout.strip())
    self.assertRegex(backup.name, r"spark-\d{8}T\d{6}Z\.db")
    self.assertEqual(0o600, stat.S_IMODE(backup.stat().st_mode))
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest -v tests.console.test_cli`
Expected: CLI module or commands are missing.

- [ ] **Step 3: Implement safe operational commands**

```python
def import_legacy(path: Path, owner_username: str) -> ImportSummary:
    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload:
        account = accounts.create_encrypted(owner_username, item["username"], item["cookies"])
        for target in item.get("targets", []):
            tasks.create(owner_username, account.id, target, "09:00", "今日火花")
    return ImportSummary(accounts=len(payload), tasks=task_count)
```

Do not print source JSON, Cookie keys, Cookie counts, encrypted blobs, or environment data. `verify-account --no-send` may log only login valid/invalid and exact-target found/not-found.

- [ ] **Step 4: Write exact operations and rollback documentation**

Document secret generation with binary files, loopback-only start, admin creation, user creation, legacy import, no-send probes, SQLite backup/restore, old timer cutover, worker stop, and old timer rollback. State that domain/TLS and any firewall change require separate approval.

- [ ] **Step 5: Run focused and full tests and verify GREEN**

Run: `python -m unittest -v tests.console.test_cli && python -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 6: Commit operational tooling**

```bash
git add spark_console/cli.py tests/console/test_cli.py docs/console-operations.md
git commit -m "feat: add secure console operations tooling"
```

---

### Task 9: Isolated Docker image and Compose project

**Files:**
- Create: `Dockerfile.console`
- Create: `compose.console.yml`
- Create: `.dockerignore`
- Create: `.env.console.example`
- Create: `tests/console/test_deployment_contract.py`

**Interfaces:**
- Consumes: `python -m spark_console.web.app`, `python -m spark_console.worker`, CLI, settings, and requirements from previous tasks.
- Produces: shared immutable image, loopback web container, internal worker container, private volume/network, health checks, and resource/log limits.

- [ ] **Step 1: Write failing deployment-contract tests**

```python
def test_compose_is_loopback_only_and_does_not_reference_bps(self):
    compose = yaml.safe_load(Path("compose.console.yml").read_text())
    self.assertEqual(["127.0.0.1:8899:8899"], compose["services"]["spark-web"]["ports"])
    serialized = json.dumps(compose)
    self.assertNotIn("bps", serialized.lower())
    self.assertNotIn("/var/run/docker.sock", serialized)
    self.assertNotIn("ports", compose["services"]["spark-worker"])

def test_worker_is_single_concurrency_and_resource_limited(self):
    worker = compose["services"]["spark-worker"]
    self.assertEqual("1", worker["environment"]["SPARK_WORKER_CONCURRENCY"])
    self.assertEqual("768m", worker["mem_limit"])
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest -v tests.console.test_deployment_contract`
Expected: `compose.console.yml` is missing.

- [ ] **Step 3: Implement shared Playwright image**

```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble
WORKDIR /app
COPY requirements.txt requirements-console.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-console.txt
RUN useradd --create-home --uid 10001 spark
COPY --chown=spark:spark . /app
USER spark
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-m", "spark_console.web.app"]
```

- [ ] **Step 4: Implement isolated Compose configuration**

```yaml
name: douyin-spark-console
services:
  spark-web:
    build: {context: ., dockerfile: Dockerfile.console}
    ports: ["127.0.0.1:8899:8899"]
    mem_limit: 256m
    cpus: 0.50
    read_only: true
    tmpfs: ["/tmp:rw,noexec,nosuid,size=64m"]
  spark-worker:
    build: {context: ., dockerfile: Dockerfile.console}
    command: ["python", "-m", "spark_console.worker"]
    environment: {SPARK_WORKER_CONCURRENCY: "1"}
    mem_limit: 768m
    cpus: 1.00
    read_only: true
    tmpfs: ["/tmp:rw,noexec,nosuid,size=256m"]
```

Complete both services with the private data volume, private network, read-only secret bind mounts, non-root UID, `no-new-privileges`, JSON log rotation (`10m`, 3 files), health checks, and `restart: unless-stopped`. Do not add BPS networks, Docker socket, public ports, or secret values.

- [ ] **Step 5: Verify Docker contracts and build locally**

Run: `python -m unittest -v tests.console.test_deployment_contract && docker compose -f compose.console.yml config && docker build -f Dockerfile.console -t douyin-spark-console:test .`
Expected: tests pass, Compose config succeeds without secret values, and image builds successfully.

- [ ] **Step 6: Run the complete test suite in the image**

Run: `docker run --rm douyin-spark-console:test python -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 7: Commit deployment artifacts**

```bash
git add Dockerfile.console compose.console.yml .dockerignore .env.console.example tests/console/test_deployment_contract.py
git commit -m "feat: containerize isolated spark console"
```

---

### Task 10: Repository documentation, security regression, and release verification

**Files:**
- Modify: `README.md`
- Create: `tests/console/test_secret_regression.py`
- Modify: `docs/superpowers/specs/2026-08-25-douyin-spark-multiuser-design.md`
- Modify: `docs/superpowers/plans/2026-08-25-douyin-spark-multiuser.md`

**Interfaces:**
- Consumes: every prior task deliverable.
- Produces: documented setup boundaries, full verification evidence, clean feature branch, and push-ready commits.

- [ ] **Step 1: Add a failing secret-regression test**

```python
def test_tracked_files_contain_no_runtime_secret_files(self):
    tracked = subprocess.check_output(["git", "ls-files"], text=True).splitlines()
    forbidden = {"usersData.json", "spark.db", "cookie.key", "session.key", ".env.console"}
    self.assertTrue(forbidden.isdisjoint(Path(path).name for path in tracked))

def test_example_files_contain_no_credential_values(self):
    text = Path(".env.console.example").read_text(encoding="utf-8")
    self.assertNotRegex(text, r"(?i)(cookie|password|token|secret)=.+")
```

- [ ] **Step 2: Run test and verify RED if ignore rules or docs are incomplete**

Run: `python -m unittest -v tests.console.test_secret_regression`
Expected: fail until runtime paths and example values are excluded or sanitized.

- [ ] **Step 3: Complete README and ignore rules**

Document architecture, role model, loopback-only behavior, minimum 5 GiB disk gate, serial Worker, domain/TLS prerequisite, BPS isolation, local tests, image build, operations guide, and platform-risk disclaimer. Add all runtime database, key, backup, environment, log, screenshot, and Cookie paths to `.gitignore` and `.dockerignore`.

- [ ] **Step 4: Run fresh full verification**

Run:

```bash
python -m unittest discover -s tests -v
python -m compileall -q core spark_console tests
docker compose -f compose.console.yml config
docker run --rm douyin-spark-console:test python -m unittest discover -s tests -v
git diff --check
git status --short
```

Expected: zero test failures, zero compile failures, valid Compose output, zero whitespace errors, and only intended tracked changes.

- [ ] **Step 5: Commit documentation and verification guards**

```bash
git add README.md .gitignore .dockerignore docs tests/console/test_secret_regression.py
git commit -m "docs: document multiuser console operations"
```

- [ ] **Step 6: Rebase safely and re-run verification**

```bash
git fetch origin
git rebase origin/main
python -m unittest discover -s tests -v
git diff --check "$(git merge-base origin/main HEAD)" HEAD
```

Expected: rebase succeeds and all tests pass against current `origin/main`.

- [ ] **Step 7: Push the feature branch**

```bash
git push -u origin feat/multiuser-console
```

Expected: remote branch is created without changing `main`.

---

### Task 11: Server preflight, private deployment, migration, and controlled cutover

**Files:**
- Server target: `/opt/douyin-spark-console`
- Server secrets: `/opt/douyin-spark-console/secrets/cookie.key`, `/opt/douyin-spark-console/secrets/session.key`
- Server data: Docker volume managed by `douyin-spark-console`
- Existing rollback assets: `/opt/douyin-spark-flow`, `douyin-spark-wz.timer`, `douyin-spark-gsy.timer`

**Interfaces:**
- Consumes: pushed feature branch, verified image/Compose, CLI, operations guide, and explicit user authority for cutover actions.
- Produces: loopback-only healthy deployment, imported encrypted legacy accounts, no-send validation, and a reversible migration state.

- [ ] **Step 1: Capture read-only preflight evidence**

Run on server: Docker container IDs/images/health, `curl http://127.0.0.1:8888/`, `free -h`, `df -h /`, Docker disk usage, open ports, current timers, system clock offset, and target path resolution.
Expected: five BPS containers unchanged, MySQL healthy, HTTP response unchanged, at least 5 GiB free, and no existing `/opt/douyin-spark-console` collision.

- [ ] **Step 2: Stop if time or storage gate fails**

If UTC offset exceeds 5 seconds, report the exact offset and request separate approval for time-service repair. If free disk is below 5 GiB, stop without building or pruning. Do not encode a schedule offset workaround.

- [ ] **Step 3: Install the private deployment without enabling Worker**

Clone/fetch `feat/multiuser-console` into `/opt/douyin-spark-console`, create root-owned `0600` binary key files, create a sanitized environment file, build the image, and start only `spark-web` bound to `127.0.0.1:8899`.
Expected: `/health/ready` is 200 over loopback; no public listener and no BPS resource changes.

- [ ] **Step 4: Create administrator and import legacy accounts**

Use CLI prompts for the initial admin password so it is not placed in command history. Import WZ and GSY JSON files by path; confirm only account/task totals are printed and the database contains no plaintext Cookie fragments.

- [ ] **Step 5: Run no-send account validation**

Run `verify-account --no-send` for each imported account.
Expected: login valid and exact target found for both accounts; no editor interaction, message submission, screenshot with private chat contents, or service start.

- [ ] **Step 6: Present cutover checkpoint and wait for explicit approval**

Report web/worker health, no-send results, BPS before/after evidence, time status, rollback commands, and the exact old timers that would be disabled. Do not disable timers or start Worker before approval.

- [ ] **Step 7: Perform approved cutover and verify**

After approval, stop/disable the two old timers, start `spark-worker`, verify queue state and next-run times, and recheck all five BPS containers plus HTTP status. Do not delete old unit files, legacy configs, volumes, or backups.

- [ ] **Step 8: Public HTTPS remains gated on domain input**

Keep `spark-web` on loopback until the user supplies a domain resolving to the server and separately approves TLS/firewall changes. Do not expose port 8899 publicly or accept credentials over HTTP.
