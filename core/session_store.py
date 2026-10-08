"""Account-scoped browser authentication state; never extends cookie expiry."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import uuid

_LOCK = threading.RLock()


class SessionStore:
    def __init__(self, unique_id, cookies, fingerprint='', directory=None):
        from app.paths import APP_DIR
        self.directory = Path(directory or os.getenv('DOUYIN_SESSION_DIR') or APP_DIR / 'sessions')
        account = str(unique_id).strip().upper()
        self.enabled = bool(account)
        self.path = self.directory / (hashlib.sha256(account.encode()).hexdigest() + '.json')
        # Ignore sameSite sanitization and formatting differences between GUI/CLI.
        canonical = [{k:v for k,v in c.items() if k != 'sameSite'} for c in cookies]
        canonical.sort(key=lambda c:(c.get('domain',''), c.get('path',''), c.get('name','')))
        self.source = hashlib.sha256(json.dumps([canonical, fingerprint], sort_keys=True).encode()).hexdigest()
        self.revision = None

    @contextmanager
    def _locked(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _LOCK:
            with open(self.path.with_suffix('.lock'), 'a+b') as lock:
                if os.name == 'posix':
                    import fcntl
                    os.chmod(lock.name, 0o600)
                    fcntl.flock(lock, fcntl.LOCK_EX)
                else:
                    import msvcrt
                    if lock.tell() == 0:
                        lock.write(b'0'); lock.flush()
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    if os.name == 'posix':
                        fcntl.flock(lock, fcntl.LOCK_UN)
                    else:
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(data.get('state'), dict):
                return data
        except (OSError, ValueError, AttributeError):
            pass
        return {}

    def load(self):
        if not self.enabled:
            return None
        with self._locked():
            data = self._read()
            self.revision = data.get('revision')
            return data.get('state') if data.get('source') == self.source else None

    def save(self, state):
        if not self.enabled or not isinstance(state, dict):
            return False
        cookies = state.get('cookies') or []
        if not any(c.get('name') in {'sessionid','sessionid_ss','sid_tt'} and c.get('value') for c in cookies):
            return False
        with self._locked():
            # A task using an older snapshot must not overwrite a newer AI snapshot.
            current = self._read()
            if current.get('revision') != self.revision:
                return False
            revision = uuid.uuid4().hex
            fd, name = tempfile.mkstemp(prefix='.session-', dir=self.directory)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump({'source':self.source, 'revision':revision, 'state':state}, f, ensure_ascii=False)
                    f.flush(); os.fsync(f.fileno())
                os.replace(name, self.path)
                self.revision = revision
            finally:
                if os.path.exists(name): os.unlink(name)
        return True


def capture_state(context):
    try:
        return context.storage_state(indexed_db=True)
    except TypeError:
        return context.storage_state()
