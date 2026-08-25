# Douyin QR Binding and Invite Registration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace manual Cookie JSON entry with a single-slot server-side Douyin QR login flow and add one-time invite registration for ordinary platform users.

**Architecture:** `spark-web` owns platform authorization, registration, invite administration, and owner-checked scan APIs. A new unexposed `spark-auth` process claims one SQLite-backed scan session, runs a fresh Playwright browser context, publishes an ephemeral QR image, and writes only AES-256-GCM-encrypted version-2 storage state. Existing version-1 Cookie accounts remain readable by the executor.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, SQLAlchemy 2, SQLite WAL, Playwright async API, AES-256-GCM, Docker Compose, `unittest`.

**Spec:** `docs/superpowers/specs/2026-08-25-douyin-qr-invite-registration-design.md`

## Global Constraints

- Keep platform username/password login; registration creates `role="user"` only.
- Invite codes are one-time, default to seven days, show plaintext once, and persist only a SHA-256 digest.
- Allow exactly one active Douyin login session globally; each session expires after five minutes.
- Never render or log Cookie, Token, storage state, QR history, invite plaintext, browser stack traces, or environment contents.
- `spark-auth` publishes no host port, mounts no Docker socket, and joins no BPS network.
- New credentials use encrypted version-2 Playwright storage state; version-1 Cookie arrays remain supported without migration.
- Do not start `spark-worker`, stop the old Douyin timers, or mutate existing BPS containers during deployment.
- Every production behavior change follows a failing-test-first red/green cycle.

## File Map

- `spark_console/models.py`: persistent invite, scan-session, and additive account-identity records.
- `spark_console/services/invites.py`: invite generation, digest lookup, atomic consumption, listing, and revocation.
- `spark_console/services/scan_sessions.py`: global-slot state machine and owner-safe public projections.
- `spark_console/services/accounts.py`: version-2 encrypted credential creation and account renaming.
- `spark_console/credentials.py`: strict parsing of version-1 and version-2 credential payloads.
- `spark_console/rate_limit.py`: single-process failed-registration sliding window.
- `spark_console/web/auth.py`: shared authenticated-user and CSRF helpers extracted from the oversized app factory.
- `spark_console/web/registration_routes.py`: public invite-registration endpoints.
- `spark_console/web/account_scan_routes.py`: owner-checked start/status/QR/cancel endpoints.
- `spark_console/auth_scanner.py`: Playwright page adapter that emits QR and authenticated storage state.
- `spark_console/auth_worker.py`: singleton scan-session claim/process/cleanup loop.
- `spark_console/web/app.py`: route composition and existing page integration.
- `spark_console/templates/login.html`, `register.html`, `accounts.html`, `admin.html`: registration, invite, and QR UI.
- `spark_console/static/app.css`, `account_scan.js`: modal/status presentation and polling.
- `compose.console.yml`, `.env.console.example`, `docs/console-operations.md`: isolated auth-service deployment and runbook.

---

### Task 1: Add additive invite and scan-session persistence

**Files:**
- Modify: `spark_console/models.py`
- Modify: `spark_console/db.py`
- Modify: `tests/console/test_config_db.py`

**Interfaces:**
- Produces: `InviteCode`, `DouyinLoginSession`, `DouyinAccountIdentity`, `ScanStatus`, and a database-enforced unique active `slot="global"`.
- Consumes: existing `Base`, `User`, `DouyinAccount`, `utc_now()`, and additive `Base.metadata.create_all()` initialization.

- [ ] **Step 1: Write failing schema tests**

Add literal tests that create one active session and prove a second `slot="global"` insert raises `IntegrityError`, while completed rows with `slot=None` coexist. Add an invite persistence test that verifies only `code_hash` exists and the foreign keys reference creator/consumer users. Assert that Douyin identity metadata lives in a new one-to-one table, because `create_all()` cannot add a column to the already deployed `douyin_accounts` table.

```python
def test_schema_allows_only_one_global_scan_slot(self):
    with session_scope(self.engine) as session:
        owner = User(username="owner", password_hash="hash", role="user")
        session.add(owner)
        session.flush()
        session.add(DouyinLoginSession(owner_user_id=owner.id, slot="global", status="queued"))
    with self.assertRaises(IntegrityError):
        with session_scope(self.engine) as session:
            session.add(DouyinLoginSession(owner_user_id=owner.id, slot="global", status="queued"))

def test_invite_model_has_no_plaintext_code_column(self):
    self.assertIn("code_hash", InviteCode.__table__.columns)
    self.assertNotIn("code", InviteCode.__table__.columns)
```

- [ ] **Step 2: Run the schema tests and verify RED**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m unittest tests.console.test_config_db -v
```

Expected: import or attribute failure because `InviteCode` and `DouyinLoginSession` do not exist.

- [ ] **Step 3: Implement additive models**

Add a string enum and models with explicit indexes and foreign keys. Use a nullable unique `slot` column so SQLite permits many terminal `NULL` rows but one `global` row.

```python
class ScanStatus(StrEnum):
    QUEUED = "queued"
    LOADING_QR = "loading_qr"
    AWAITING_SCAN = "awaiting_scan"
    CONFIRMING = "confirming"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"

class InviteCode(Base):
    __tablename__ = "invite_codes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

class DouyinLoginSession(Base):
    __tablename__ = "douyin_login_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    slot: Mapped[str | None] = mapped_column(String(16), unique=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=ScanStatus.QUEUED)
    qr_png: Mapped[bytes | None] = mapped_column(LargeBinary)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("douyin_accounts.id", ondelete="SET NULL"))
    error_code: Mapped[str | None] = mapped_column(String(48))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class DouyinAccountIdentity(Base):
    __tablename__ = "douyin_account_identities"
    account_id: Mapped[str] = mapped_column(
        ForeignKey("douyin_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    douyin_unique_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
```

- [ ] **Step 4: Run schema tests and verify GREEN**

Run the command from Step 2. Expected: all config/database tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add spark_console/models.py spark_console/db.py tests/console/test_config_db.py
git commit -m "feat: add invite and QR session schema"
```

---

### Task 2: Implement one-time invite and registration domain services

**Files:**
- Create: `spark_console/services/invites.py`
- Create: `spark_console/rate_limit.py`
- Modify: `spark_console/services/users.py`
- Create: `tests/console/test_invites.py`

**Interfaces:**
- Produces: `InviteService.create(actor_id, lifetime) -> (InviteCode, str)`, `InviteService.consume(code, user_id)`, `InviteService.list_all()`, `InviteService.revoke(actor_id, invite_id)`, `validate_registration_password(password)`, and `FailedAttemptLimiter.allow(key)` / `record_failure(key)`.
- Consumes: Task 1 `InviteCode`; existing `UserService`, `PasswordService`, `AuditService`, and SQLAlchemy session transaction.

- [ ] **Step 1: Write failing invite-service tests**

Cover plaintext shown once but absent from the row/audit, seven-day expiration, one-time consumption, revoke, expired rejection, and password rules. Use a fixed clock injected into the service.

```python
def test_invite_plaintext_is_returned_once_and_only_digest_is_stored(self):
    invite, plaintext = self.invites.create(self.admin.id)
    self.assertGreaterEqual(len(plaintext), 24)
    self.assertNotEqual(plaintext, invite.code_hash)
    self.assertEqual(hashlib.sha256(plaintext.encode()).hexdigest(), invite.code_hash)
    self.assertNotIn(plaintext, " ".join(e.detail or "" for e in self.session.scalars(select(AuditEvent))))

def test_invite_can_be_consumed_only_once(self):
    invite, plaintext = self.invites.create(self.admin.id)
    self.invites.consume(plaintext, self.user.id)
    with self.assertRaises(ValidationError):
        self.invites.consume(plaintext, self.other.id)
```

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
& '.\.venv\Scripts\python.exe' -m unittest tests.console.test_invites -v
```

Expected: module import failure because `services.invites` does not exist.

- [ ] **Step 3: Implement invite service and password validator**

Use high-entropy tokens, digest lookup, timezone-aware comparisons, stable validation errors, and audit events without detail payloads.

```python
def _digest(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()

def validate_registration_password(password: str) -> None:
    if len(password) < 10 or not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise ValidationError("注册信息或邀请码无效")

def create(self, actor_id: str, lifetime: timedelta = timedelta(days=7)) -> tuple[InviteCode, str]:
    plaintext = secrets.token_urlsafe(24)
    invite = InviteCode(code_hash=_digest(plaintext), created_by_user_id=actor_id, expires_at=self.now() + lifetime)
    self.session.add(invite)
    self.session.flush()
    self.audit.write(actor_id, "invite.created", "invite_code", invite.id)
    return invite, plaintext
```

`consume` must first find the digest, then perform one conditional `UPDATE` whose `WHERE` requires matching ID, `used_at IS NULL`, `revoked_at IS NULL`, and `expires_at > now`. Require `rowcount == 1`; a concurrent consumer receives the same `ValidationError` instead of overwriting the first user.

```python
result = self.session.execute(
    update(InviteCode)
    .where(
        InviteCode.id == invite.id,
        InviteCode.used_at.is_(None),
        InviteCode.revoked_at.is_(None),
        InviteCode.expires_at > now,
    )
    .values(used_by_user_id=user_id, used_at=now)
)
if result.rowcount != 1:
    raise ValidationError("注册信息或邀请码无效")
```

- [ ] **Step 4: Implement the single-process failed-attempt limiter**

Use a deque per key and count only failed submissions. Make time injectable and avoid storing submitted values.

```python
class FailedAttemptLimiter:
    def __init__(self, limit: int = 10, window: timedelta = timedelta(minutes=10), now=utc_now):
        self.limit, self.window, self.now = limit, window, now
        self.attempts: dict[str, deque[datetime]] = defaultdict(deque)

    def _prune(self, key: str) -> deque[datetime]:
        values = self.attempts[key]
        cutoff = self.now() - self.window
        while values and values[0] <= cutoff:
            values.popleft()
        return values

    def allow(self, key: str) -> bool:
        return len(self._prune(key)) < self.limit

    def record_failure(self, key: str) -> None:
        self._prune(key).append(self.now())

    def clear(self, key: str) -> None:
        self.attempts.pop(key, None)
```

- [ ] **Step 5: Run invite tests and full service tests**

```powershell
& '.\.venv\Scripts\python.exe' -m unittest tests.console.test_invites tests.console.test_services -v
```

Expected: all tests pass and no plaintext invitation appears in test serialization.

- [ ] **Step 6: Commit Task 2**

```bash
git add spark_console/services/invites.py spark_console/rate_limit.py spark_console/services/users.py tests/console/test_invites.py
git commit -m "feat: add one-time invite registration services"
```

---

### Task 3: Add public registration and administrator invite UI

**Files:**
- Create: `spark_console/web/registration_routes.py`
- Create: `spark_console/web/auth.py`
- Create: `spark_console/templates/register.html`
- Modify: `spark_console/web/app.py`
- Modify: `spark_console/templates/login.html`
- Modify: `spark_console/templates/admin.html`
- Modify: `spark_console/static/app.css`
- Create: `tests/console/test_registration_web.py`
- Modify: `tests/console/test_web_user.py`

**Interfaces:**
- Produces: `WebAuth.current(request, db, allow_change=False)`, `WebAuth.csrf(record, supplied)`, `WebAuth.user_context(request, db)`, `WebAuth.admin_context(request, db)`; `build_registration_router(engine, passwords, limiter, auth, page) -> APIRouter`; `GET/POST /register`; admin `POST /admin/invites` and `POST /admin/invites/{id}/revoke`.
- Consumes: Task 2 invite/password/limiter APIs and existing platform session/CSRF/admin checks.

- [ ] **Step 1: Write failing Web behavior tests**

Test the login-page link, successful ordinary-user registration, password mismatch, invalid/used invite with identical public text, rate limiting, admin-only generation, one-time display, and revoke CSRF.

```python
def test_valid_invite_registers_ordinary_user_then_redirects_to_login(self):
    response = self.client.post("/register", data={
        "username": "newfriend",
        "password": "StrongPass10",
        "password_confirmation": "StrongPass10",
        "invite_code": self.invite_plaintext,
    }, follow_redirects=False)
    self.assertEqual(303, response.status_code)
    self.assertEqual("/login?registered=1", response.headers["location"])
    with Session(self.engine) as session:
        user = session.scalar(select(User).where(User.username == "newfriend"))
        self.assertEqual("user", user.role)
        self.assertFalse(user.must_change_password)
```

- [ ] **Step 2: Run Web tests and verify RED**

```powershell
& '.\.venv\Scripts\python.exe' -m unittest tests.console.test_registration_web -v
```

Expected: 404 for `/register` and missing invite controls.

- [ ] **Step 3: Extract reusable Web authorization helpers**

Move the existing nested `current`, `csrf`, `user_context`, and `admin_context` behavior without semantic changes into `WebAuth`. Update every existing route call site and run `tests.console.test_web_user` before adding registration routes.

```python
class WebAuth:
    def __init__(self, sessions: SessionService):
        self.sessions = sessions

    def current(self, request: Request, db: Session, allow_change: bool = False) -> tuple[User, WebSession]:
        raw = request.cookies.get("spark_session")
        if not raw:
            raise HTTPException(401)
        record = db.scalar(select(WebSession).where(
            WebSession.token_hash == self.sessions.token_hash(raw)
        ))
        if record is None or _aware(record.expires_at) <= datetime.now(timezone.utc):
            raise HTTPException(401)
        user = db.get(User, record.user_id)
        if user is None or user.status != "active":
            raise HTTPException(401)
        if user.must_change_password and not allow_change:
            raise HTTPException(409, "password-change-required")
        return user, record

    def csrf(self, record: WebSession, supplied: str) -> None:
        if not supplied or not secrets.compare_digest(record.csrf_token, supplied):
            raise HTTPException(403, "CSRF validation failed")

    def user_context(self, request: Request, db: Session) -> tuple[User, WebSession, dict]:
        user, record = self.current(request, db)
        return user, record, {"user": user, "csrf_token": record.csrf_token, "is_admin": user.role == "admin"}

    def admin_context(self, request: Request, db: Session) -> tuple[User, WebSession, dict]:
        user, record = self.current(request, db)
        if user.role != "admin":
            raise HTTPException(404)
        return user, record, {"user": user, "csrf_token": record.csrf_token, "is_admin": True}
```

- [ ] **Step 4: Implement registration router**

Create the user and consume the invite in the same `session_scope`. Never distinguish username, password, or invite failures in the response.

```python
@router.post("/register")
def register(request: Request, username: str = Form(), password: str = Form(),
             password_confirmation: str = Form(), invite_code: str = Form()):
    key = registration_client_key(request)
    if not limiter.allow(key):
        return page(request, "register.html", 429, error=PUBLIC_ERROR)
    try:
        with session_scope(engine) as db:
            validate_registration_password(password)
            if password != password_confirmation:
                raise ValidationError(PUBLIC_ERROR)
            user, _ = UserService(db, passwords, AuditService(db)).create(username, password, "user")
            user.must_change_password = False
            InviteService(db, AuditService(db)).consume(invite_code, user.id)
    except (ValidationError, Conflict, IntegrityError):
        limiter.record_failure(key)
        return page(request, "register.html", 400, error=PUBLIC_ERROR)
    limiter.clear(key)
    return RedirectResponse("/login?registered=1", 303)
```

`registration_client_key` must parse the Nginx-overwritten `X-Real-IP` value with `ipaddress.ip_address`; fall back to `request.client.host` when absent/invalid. This is safe for the deployed topology because port 8899 remains loopback-only and public requests pass through the trusted local Nginx server.

- [ ] **Step 5: Add login/register and admin invite templates**

Add a secondary registration link, a standalone register card, and admin invite list/form. Render `one_time_invite` only in the immediate generation response. Add responsive styles using existing tokens; do not add a frontend dependency.

- [ ] **Step 6: Run registration and existing Web tests**

```powershell
& '.\.venv\Scripts\python.exe' -m unittest tests.console.test_registration_web tests.console.test_web_user -v
```

Expected: registration/admin invite tests and existing login/CSRF tests all pass.

- [ ] **Step 7: Commit Task 3**

```bash
git add spark_console/web/auth.py spark_console/web/registration_routes.py spark_console/web/app.py spark_console/templates/register.html spark_console/templates/login.html spark_console/templates/admin.html spark_console/static/app.css tests/console/test_registration_web.py tests/console/test_web_user.py
git commit -m "feat: add invite registration pages"
```

---

### Task 4: Add versioned encrypted Playwright credentials

**Files:**
- Create: `spark_console/credentials.py`
- Modify: `spark_console/services/accounts.py`
- Modify: `spark_console/executor.py`
- Modify: `spark_console/models.py`
- Modify: `spark_console/worker.py`
- Modify: `tests/console/test_services.py`
- Create: `tests/console/test_credentials.py`
- Modify: `tests/console/test_scheduler_worker.py`
- Modify: `tests/console/test_secret_regression.py`

**Interfaces:**
- Produces: `CredentialPayload.parse(raw: bytes, version: int)`, `CredentialPayload.context_options()`, `AccountService.create_from_storage_state(owner_id: str, display_name: str, storage_state: dict, douyin_unique_id: str | None = None)`, and `AccountService.rename_owned(owner_id: str, account_id: str, display_name: str)`.
- Consumes: `CookieCipher`, Playwright `browser.new_context(**options)`, account `cookie_version`, and existing version-1 encrypted Cookie rows.

- [ ] **Step 1: Write failing version-compatibility tests**

Use literal version-1 and version-2 bytes. Assert version 1 produces an empty context plus `cookies_to_add`, version 2 produces `{"storage_state": state}`, empty Cookie arrays fail, encrypted rows contain neither cookie nor local-storage marker, and list projections expose no credential fields.

```python
def test_version_two_storage_state_becomes_context_option(self):
    raw = b'{"version":2,"storage_state":{"cookies":[{"name":"sid","value":"secret","domain":".douyin.com","path":"/"}],"origins":[]}}'
    payload = CredentialPayload.parse(raw, 2)
    self.assertEqual("secret", payload.context_options()["storage_state"]["cookies"][0]["value"])
    self.assertEqual([], payload.cookies_to_add())
```

- [ ] **Step 2: Run credential tests and verify RED**

```powershell
& '.\.venv\Scripts\python.exe' -m unittest tests.console.test_credentials -v
```

Expected: import failure because `spark_console.credentials` does not exist.

- [ ] **Step 3: Implement strict versioned parser and account service methods**

Reject unknown versions and malformed shapes. Serialize version 2 with compact UTF-8 JSON, encrypt it immediately, set `cookie_version=2`, `validation_state="valid"`, and never pass raw state to audit details.

```python
envelope = {"version": 2, "storage_state": storage_state}
raw = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
sealed = self.cipher.encrypt(raw)
account = DouyinAccount(
    owner_user_id=owner_id,
    display_name=name,
    encrypted_cookies=sealed.ciphertext,
    cookie_nonce=sealed.nonce,
    cookie_version=2,
    validation_state="valid",
    last_verified_at=utc_now(),
)
self.session.add(account)
self.session.flush()
self.session.add(
    DouyinAccountIdentity(account_id=account.id, douyin_unique_id=normalized_unique_id)
)
```

- [ ] **Step 4: Update executor for both versions**

Decrypt with the current bytearray discipline, parse by `cookie_version`, create the context using version-2 options, and add version-1 cookies only after context creation. Keep public error codes unchanged.

```python
payload = CredentialPayload.parse(bytes(cookie_payload), credential_version)
context = await browser.new_context(**payload.context_options())
legacy_cookies = payload.cookies_to_add()
if legacy_cookies:
    await context.add_cookies(legacy_cookies)
```

Change the executor signature to `execute(cookie_payload, target, message, credential_version=1)` and update the worker call site to pass `account.cookie_version`.

- [ ] **Step 5: Run credential, service, worker, and secret tests**

```powershell
& '.\.venv\Scripts\python.exe' -m unittest tests.console.test_credentials tests.console.test_services tests.console.test_scheduler_worker tests.console.test_secret_regression -v
```

Expected: both credential versions pass and serialized pages/log fixtures contain no test secret.

- [ ] **Step 6: Commit Task 4**

```bash
git add spark_console/credentials.py spark_console/services/accounts.py spark_console/executor.py spark_console/worker.py spark_console/models.py tests/console/test_credentials.py tests/console/test_services.py tests/console/test_scheduler_worker.py tests/console/test_secret_regression.py
git commit -m "feat: support encrypted browser storage state"
```

---

### Task 5: Implement the global scan-session state machine

**Files:**
- Create: `spark_console/services/scan_sessions.py`
- Create: `tests/console/test_scan_sessions.py`

**Interfaces:**
- Produces: `ScanSessionService.start(owner_id)`, `claim_next()`, `publish_qr(id, png)`, `mark_confirming(id)`, `complete(id, account_id)`, `fail(id, code)`, `cancel_owned(owner_id, id)`, `get_owned(owner_id, id)`, `expire_stale()`, and `public_status(session, now)`.
- Consumes: Task 1 `DouyinLoginSession`/`ScanStatus`, stable public error codes, and UTC clock injection.

- [ ] **Step 1: Write failing transition and isolation tests**

Test one global slot, owner-only access/cancel, legal transitions, PNG clearing in every terminal state, timeout cleanup, worker startup cleanup, and a public projection that omits `qr_png`, owner ID, and database internals.

```python
def test_cancel_clears_qr_and_releases_global_slot(self):
    scan = self.service.start(self.owner.id)
    self.service.publish_qr(scan.id, b"png-bytes")
    cancelled = self.service.cancel_owned(self.owner.id, scan.id)
    self.assertEqual(ScanStatus.CANCELLED, cancelled.status)
    self.assertIsNone(cancelled.qr_png)
    self.assertIsNone(cancelled.slot)
    self.service.start(self.other.id)
```

- [ ] **Step 2: Run state-machine tests and verify RED**

```powershell
& '.\.venv\Scripts\python.exe' -m unittest tests.console.test_scan_sessions -v
```

Expected: module import failure.

- [ ] **Step 3: Implement guarded transitions**

Define an explicit transition map and one terminal helper. Convert `IntegrityError` from `slot="global"` insertion into `Conflict("slot_busy")` without retrying.

```python
ALLOWED = {
    ScanStatus.QUEUED: {ScanStatus.LOADING_QR, ScanStatus.CANCELLED, ScanStatus.EXPIRED, ScanStatus.FAILED},
    ScanStatus.LOADING_QR: {ScanStatus.AWAITING_SCAN, ScanStatus.CANCELLED, ScanStatus.EXPIRED, ScanStatus.FAILED},
    ScanStatus.AWAITING_SCAN: {ScanStatus.CONFIRMING, ScanStatus.SUCCEEDED, ScanStatus.CANCELLED, ScanStatus.EXPIRED, ScanStatus.FAILED},
    ScanStatus.CONFIRMING: {ScanStatus.SUCCEEDED, ScanStatus.CANCELLED, ScanStatus.EXPIRED, ScanStatus.FAILED},
}
```

`publish_qr` accepts only PNG signature bytes beginning with `b"\x89PNG\r\n\x1a\n"` and caps size at 1 MiB.

- [ ] **Step 4: Run state-machine tests and verify GREEN**

Run the command from Step 2. Expected: all state, isolation, and cleanup tests pass.

- [ ] **Step 5: Commit Task 5**

```bash
git add spark_console/services/scan_sessions.py tests/console/test_scan_sessions.py
git commit -m "feat: add QR scan session state machine"
```

---

### Task 6: Build the isolated Playwright authentication worker

**Files:**
- Create: `spark_console/auth_scanner.py`
- Create: `spark_console/auth_worker.py`
- Create: `tests/console/test_auth_scanner.py`
- Create: `tests/console/test_auth_worker.py`

**Interfaces:**
- Produces: `DouyinQrScanner.run(on_qr, on_confirming, cancelled) -> ScannedAccount`; `ScannedAccount(display_name, unique_id, storage_state)`; `AuthWorker.run_once() -> bool`; module entry point `python -m spark_console.auth_worker`.
- Consumes: Task 4 `AccountService.create_from_storage_state`, Task 5 state-machine APIs, Playwright async API, and settings/database/encryption construction used by the existing worker.

- [ ] **Step 1: Write failing scanner-boundary tests**

Build small async fakes for browser/page/locator only at the Playwright boundary. Verify QR PNG callback, confirming callback, authenticated account extraction, storage-state return, timeout mapping, extra-verification mapping, cancellation, and unconditional context/browser cleanup. Assertions target scanner results and callbacks, not mock call existence.

```python
async def test_scanner_returns_account_after_qr_and_mobile_confirmation(self):
    result = await self.scanner.run(self.qr_images.append, self.confirmations.append, lambda: False)
    self.assertEqual("测试昵称", result.display_name)
    self.assertEqual("douyin-123", result.unique_id)
    self.assertTrue(result.storage_state["cookies"])
    self.assertTrue(self.qr_images[0].startswith(b"\x89PNG"))
```

- [ ] **Step 2: Run scanner/worker tests and verify RED**

```powershell
& '.\.venv\Scripts\python.exe' -m unittest tests.console.test_auth_scanner tests.console.test_auth_worker -v
```

Expected: import failure for both new modules.

- [ ] **Step 3: Implement scanner with centralized selectors and sanitized errors**

Keep selectors in named tuples at the top of `auth_scanner.py`. Navigate to `https://creator.douyin.com/`, locate a visible QR `img` or `canvas` inside the login panel, screenshot only that element, and wait concurrently for authenticated-home, confirming, extra-verification, cancellation, or deadline signals. Raise only typed internal exceptions:

```python
QR_SELECTORS = (
    'img[alt*="二维码"]',
    '[class*="qrcode"] img',
    '[class*="qr-code"] img',
    '[class*="qrcode"] canvas',
    '[class*="qr-code"] canvas',
)
AUTHENTICATED_SELECTOR = 'xpath=//*[contains(@id,"garfish_app_for_douyin_creator_pc_home")]'
DISPLAY_NAME_SELECTOR = 'xpath=//*[contains(@id,"garfish_app_for_douyin_creator_pc_home")]/div/div[2]/div/div[2]/div[1]/div[2]/div[1]/div[1]/div[1]'
UNIQUE_ID_SELECTOR = 'xpath=//*[contains(@id,"garfish_app_for_douyin_creator_pc_home")]/div/div[2]/div/div[2]/div[1]/div[2]/div[1]/div[3]'
CONFIRMING_TEXT = ("扫码成功", "请在手机上确认", "已扫码")
VERIFICATION_TEXT = ("安全验证", "验证码", "请完成验证")

class QrLoadFailed(Exception): pass
class LoginTimedOut(Exception): pass
class VerificationRequired(Exception): pass
class ScanCancelled(Exception): pass

@dataclass(frozen=True)
class ScannedAccount:
    display_name: str
    unique_id: str | None
    storage_state: dict[str, object]
```

Do not print page HTML, URLs after redirects, storage state, or exception reprs.

- [ ] **Step 4: Implement one-session auth worker**

On startup call `expire_stale()`. `run_once()` claims one row, passes callbacks that open fresh DB sessions for state updates, checks cancellation between waits, validates non-empty cookies, encrypts/account-creates, then completes the session in one final transaction.

Map typed scanner exceptions to exact public codes and map all other exceptions to `automation_failed` while logging only session ID and code.

```python
ERROR_CODES = {
    QrLoadFailed: "qr_load_failed",
    LoginTimedOut: "login_timeout",
    VerificationRequired: "verification_required",
    ScanCancelled: "cancelled",
}
```

- [ ] **Step 5: Run scanner/worker and secret-regression tests**

```powershell
& '.\.venv\Scripts\python.exe' -m unittest tests.console.test_auth_scanner tests.console.test_auth_worker tests.console.test_secret_regression -v
```

Expected: lifecycle/error tests pass and captured logs contain none of the fixture Cookie, Token, QR, or storage-state values.

- [ ] **Step 6: Commit Task 6**

```bash
git add spark_console/auth_scanner.py spark_console/auth_worker.py tests/console/test_auth_scanner.py tests/console/test_auth_worker.py
git commit -m "feat: add isolated Douyin QR auth worker"
```

---

### Task 7: Replace manual Cookie form with owner-safe QR APIs and modal UI

**Files:**
- Create: `spark_console/web/account_scan_routes.py`
- Create: `spark_console/static/account_scan.js`
- Modify: `spark_console/web/app.py`
- Modify: `spark_console/templates/accounts.html`
- Modify: `spark_console/static/app.css`
- Modify: `tests/console/test_web_user.py`
- Create: `tests/console/test_account_scan_web.py`

**Interfaces:**
- Produces: `POST /accounts/scan`, `GET /accounts/scan/{id}`, `GET /accounts/scan/{id}/qr`, `POST /accounts/scan/{id}/cancel`, `POST /accounts/{id}/rename`, QR modal polling every two seconds, and removal of the manual credential endpoint.
- Consumes: Task 5 `ScanSessionService`, existing platform `current`/`csrf`, and account list projection.

- [ ] **Step 1: Write failing owner/security/API tests**

Test authenticated start + CSRF, busy 409, anonymous redirect, cross-user 404, status projection, PNG content type/signature, `Cache-Control: no-store`, cancellation, owner-only rename, absence of the old `textarea name="cookies"`, and rejection of the old manual `POST /accounts` payload.

```python
def test_owner_can_fetch_no_store_qr_but_other_user_gets_404(self):
    response = self.owner_client.get(f"/accounts/scan/{self.scan.id}/qr")
    self.assertEqual(200, response.status_code)
    self.assertEqual("image/png", response.headers["content-type"])
    self.assertEqual("no-store", response.headers["cache-control"])
    self.assertEqual(b"\x89PNG\r\n\x1a\nfixture", response.content)
    self.assertEqual(404, self.other_client.get(f"/accounts/scan/{self.scan.id}/qr").status_code)
```

- [ ] **Step 2: Run account-scan Web tests and verify RED**

```powershell
& '.\.venv\Scripts\python.exe' -m unittest tests.console.test_account_scan_web -v
```

Expected: 404 routes and old manual Cookie form assertion failure.

- [ ] **Step 3: Implement owner-safe route module**

Return only `{id,status,remaining_seconds,error,message,account_id}`. Resolve messages through a fixed dictionary. QR responses use `Response(content=png, media_type="image/png", headers={"Cache-Control":"no-store"})`. Never accept owner, initial account name, status, or credential input from the browser. Remove the existing manual `POST /accounts` route completely. Add an owner-checked, CSRF-protected rename route that accepts only a 1–64-character display name and calls `AccountService.rename_owned`.

- [ ] **Step 4: Implement modal and polling JavaScript**

Use a no-dependency script loaded with `defer`. Read the CSRF token from a data attribute, create a session, set the QR `<img>` URL with a timestamp only while awaiting scan, poll every 2000 ms, stop on terminal status, and send cancel on explicit close. Use `textContent`, never `innerHTML`, for server messages.

```javascript
const TERMINAL = new Set(["succeeded", "failed", "expired", "cancelled"]);
async function pollScan(id) {
  const response = await fetch(`/accounts/scan/${id}`, {cache: "no-store"});
  const state = await response.json();
  statusNode.textContent = state.message;
  if (state.status === "awaiting_scan") qrNode.src = `/accounts/scan/${id}/qr?t=${Date.now()}`;
  if (state.status === "succeeded") window.location.reload();
  if (!TERMINAL.has(state.status)) timer = window.setTimeout(() => pollScan(id), 2000);
}
```

- [ ] **Step 5: Replace account form and style responsive modal**

Remove the Cookie textarea and copy. Add a primary scan button, modal dialog, 240px QR frame, status text, countdown, cancel button, busy state, reduced-motion compliance, and mobile layout consistent with existing design tokens. Each bound account receives a compact “修改备注” form that submits only the new display name and CSRF token.

- [ ] **Step 6: Run scan Web, user Web, and secret tests**

```powershell
& '.\.venv\Scripts\python.exe' -m unittest tests.console.test_account_scan_web tests.console.test_web_user tests.console.test_secret_regression -v
```

Expected: all routes/UI projections pass and rendered HTML contains no credential input or fixture secret.

- [ ] **Step 7: Commit Task 7**

```bash
git add spark_console/web/account_scan_routes.py spark_console/web/app.py spark_console/templates/accounts.html spark_console/static/account_scan.js spark_console/static/app.css tests/console/test_account_scan_web.py tests/console/test_web_user.py tests/console/test_secret_regression.py
git commit -m "feat: add Douyin QR binding interface"
```

---

### Task 8: Add isolated Compose deployment, operations, and end-to-end gates

**Files:**
- Modify: `compose.console.yml`
- Modify: `.env.console.example`
- Modify: `docs/console-operations.md`
- Modify: `tests/console/test_deployment_contract.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `spark-auth` Compose service running `python -m spark_console.auth_worker` with no port and bounded resources; documented backup/start/verify/rollback procedure.
- Consumes: Task 6 module entry point, shared `spark-data` volume and encryption key, existing private network, production HTTPS ingress.

- [ ] **Step 1: Write failing deployment-contract tests**

Parse the auth service block and assert command, no ports, no Docker socket, `768m`, `1.0`, read-only common settings, private network only, and no BPS text anywhere.

```python
def test_auth_service_is_unpublished_singleton_and_resource_limited(self):
    auth = self.compose.split("  spark-auth:", 1)[1].split("  spark-worker:", 1)[0]
    self.assertIn("python, -m, spark_console.auth_worker", auth)
    self.assertNotIn("ports:", auth)
    self.assertIn("mem_limit: 768m", auth)
    self.assertIn("cpus: 1.0", auth)
```

- [ ] **Step 2: Run deployment tests and verify RED**

```powershell
& '.\.venv\Scripts\python.exe' -m unittest tests.console.test_deployment_contract -v
```

Expected: missing `spark-auth` split/expectations.

- [ ] **Step 3: Add auth service and safe operational settings**

```yaml
  spark-auth:
    <<: *common
    command: [python, -m, spark_console.auth_worker]
    mem_limit: 768m
    cpus: 1.0
    tmpfs: ["/tmp:rw,noexec,nosuid,size=256m"]
```

Do not publish a port or add a health endpoint. The auth worker must exit non-zero only on unrecoverable configuration failure; per-session failures remain in the session state.

- [ ] **Step 4: Document exact deployment and rollback**

Document these production gates without embedding credentials:

```bash
docker compose --env-file .env.console -f compose.console.yml run --rm spark-web python -m spark_console.cli backup-db
docker compose --env-file .env.console -f compose.console.yml build spark-web spark-auth
docker compose --env-file .env.console -f compose.console.yml up -d --no-deps spark-web spark-auth
curl --fail https://wangze.oilu.cn/health/ready
docker compose --env-file .env.console -f compose.console.yml ps
```

Rollback stops only `spark-auth`, checks out the prior console commit, rebuilds/recreates only `spark-web`, and retains the database/volume.

- [ ] **Step 5: Run the complete local verification suite**

```powershell
$env:GITHUB_ACTIONS='true'
$env:WZ_DATA='[]'
$env:SPARK_LOG_DIR="$env:TEMP\spark-console-test-logs"
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests -v
& '.\.venv\Scripts\python.exe' -m compileall -q spark_console core utils
git diff --check
```

Expected: zero test failures, compile exit 0, diff check exit 0.

- [ ] **Step 6: Commit Task 8**

```bash
git add compose.console.yml .env.console.example docs/console-operations.md README.md tests/console/test_deployment_contract.py
git commit -m "docs: add isolated QR auth deployment"
```

- [ ] **Step 7: Publish the feature branch without touching main**

Fetch and integrate `origin/main` without history rewriting, rerun the complete suite if source changes, push `feat/multiuser-console`, and verify the remote ref equals local HEAD.

```bash
git fetch origin main
git merge --no-edit origin/main
git push origin feat/multiuser-console
git ls-remote origin refs/heads/feat/multiuser-console
```

- [ ] **Step 8: Deploy with read-only preflight and database backup**

Before mutation verify `id`, passwordless sudo, disk, memory, failed units, current timers, current BPS containers, current console commit/status, and database backup result. Build/recreate only `spark-web` and start only the new `spark-auth`; do not start `spark-worker`.

- [ ] **Step 9: Perform online automated acceptance**

Verify HTTPS registration page, login page registration link, wrong-password feedback, admin authorization, Web health, auth container running without published ports, direct 8899 still closed, five BPS containers unchanged, and both old timers active. Do not print generated invite plaintext during automated checks.

- [ ] **Step 10: Perform user-assisted real QR acceptance**

Have the administrator generate one invitation in the UI, register a disposable ordinary platform user, start QR binding, and ask the user to scan with their own Douyin App. Verify only these non-sensitive outcomes: status becomes succeeded, an account nickname appears, `cookie_version=2`, validation state is valid, QR bytes are cleared, and no credential appears in Web/container logs. Delete nothing after the test unless the user explicitly authorizes it.
