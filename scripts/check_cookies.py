"""Read-only login check; never sends chat messages or updates cookies."""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
from pathlib import Path
import tempfile
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('LOG_LEVEL', 'Error')
from app.config.env_store import load_config
from core.douyin_im import DouyinIM, JS_LOGIN_DOM
from cloakbrowser import launch
from core.session_store import SessionStore

TZ = ZoneInfo('Asia/Shanghai')
STATE = ROOT / 'cookie-health.json'
logger = logging.getLogger('cookie-health')
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())
(ROOT / 'logs').mkdir(parents=True, exist_ok=True)
handler = RotatingFileHandler(ROOT / 'logs/cookie-health.log', maxBytes=1024*1024, backupCount=3, encoding='utf-8')
logger.addHandler(handler)

def now():
    return datetime.now(TZ).isoformat(timespec='seconds')

def metadata(cookies):
    result = []
    for c in cookies:
        if c.get('name') not in {'sessionid', 'sessionid_ss', 'sid_tt', 'sid_guard'}:
            continue
        expires = c.get('expires', -1)
        result.append({'name': c['name'], 'expires_at': datetime.fromtimestamp(expires, timezone.utc).astimezone(TZ).isoformat() if isinstance(expires, (int, float)) and expires > 0 else None})
    return result

def check(account):
    cookies = json.loads(account.cookies or '[]')
    result = {'status':'unknown', 'reason':'not_checked', 'cookie_expiry_metadata':metadata(cookies)}
    if not cookies or not any(c.get('name') in {'sessionid','sessionid_ss','sid_tt'} and c.get('value') for c in cookies):
        result.update(status='invalid', reason='missing_session_cookie')
        return result
    browser = None
    try:
        args = ['--no-first-run', '--no-default-browser-check']
        if account.fingerprint:
            args.append('--fingerprint='+account.fingerprint)
        kwargs = {'headless':True, 'humanize':True, 'args':args}
        if cfg.proxy_address:
            kwargs['proxy'] = cfg.proxy_address
        browser = launch(**kwargs)
        saved = SessionStore(account.unique_id, cookies, account.fingerprint).load()
        context = browser.new_context(**({'storage_state':saved} if saved else {}))
        context.set_default_timeout(30000)
        context.set_default_navigation_timeout(30000)
        if saved is None:
            context.add_cookies([{k:v for k,v in c.items() if k != 'sameSite'} for c in cookies])
        page = context.new_page()
        im = DouyinIM(page, ready_timeout=30, timeout=30, settle_ms=0)
        gate = im.wait_ready()
        dom = page.evaluate(JS_LOGIN_DOM)
        login_prompt = page.evaluate("""() => {
            const t = document.body ? document.body.innerText : '';
            return t.includes('扫码登录') && t.includes('验证码登录');
        }""")
        result['im_status'] = gate.get('status')
        if gate.get('status') == 'READY' and not login_prompt:
            result.update(status='valid', reason='authenticated_chat_ready')
        elif gate.get('status') in {'EXPIRED','LOGGED_OUT','LOGIN_LOST'} or login_prompt or dom.get('loginVisible'):
            result.update(status='invalid', reason='login_required')
        else:
            result.update(status='unknown', reason='chat_page_or_network_unavailable')
    except Exception as exc:
        result.update(status='unknown', reason='check_exception_'+type(exc).__name__)
    finally:
        if browser is not None:
            try: browser.close()
            except Exception: pass
    return result

def merge(previous, current, checked_at):
    current['checked_at'] = checked_at
    current['last_valid_at'] = previous.get('last_valid_at')
    current['first_invalid_at'] = previous.get('first_invalid_at')
    if current['status'] == 'valid':
        current['last_valid_at'] = checked_at
        current['first_invalid_at'] = None
    elif current['status'] == 'invalid' and not current['first_invalid_at']:
        current['first_invalid_at'] = checked_at
    if current['first_invalid_at']:
        current['invalid_since_lower_bound'] = current['last_valid_at']
        current['invalid_since_upper_bound'] = current['first_invalid_at']
    return current

if __name__ == '__main__':
    cfg, _ = load_config(ROOT / '.env')
    try:
        previous = json.loads(STATE.read_text(encoding='utf-8')).get('accounts', {})
    except (FileNotFoundError, ValueError):
        previous = {}
    accounts = {}
    for account in cfg.accounts:
        old = previous.get(account.unique_id, {})
        current = check(account)
        # A temporary login popup during navigation is insufficient evidence.
        # Confirm with another fresh browser before marking saved cookies invalid.
        if current['status'] == 'invalid':
            time.sleep(5)
            confirmation = check(account)
            if confirmation['status'] != 'invalid':
                current = confirmation
            else:
                current['confirmed_by_checks'] = 2
        current['username'] = account.username
        current = merge(old, current, now())
        accounts[account.unique_id] = current
        logger.info(json.dumps({'checked_at':current['checked_at'], 'username':account.username, 'status':current['status'], 'previous_status':old.get('status'), 'reason':current['reason'], 'last_valid_at':current['last_valid_at'], 'first_invalid_at':current['first_invalid_at']}, ensure_ascii=False))
    fd, tmp = tempfile.mkstemp(prefix='.cookie-health-', dir=ROOT)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'checked_at':now(), 'timezone':'Asia/Shanghai', 'accounts':accounts}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
