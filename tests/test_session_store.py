import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from core.session_store import SessionStore, capture_state


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cookies = [{'name':'sessionid','value':'old','domain':'.douyin.com','path':'/'}]
        self.state = {'cookies':[{**self.cookies[0], 'value':'rotated'}], 'origins':[{'origin':'https://www.douyin.com','localStorage':[{'name':'token','value':'test'}]}]}

    def store(self, uid='a', cookies=None):
        return SessionStore(uid, self.cookies if cookies is None else cookies, 'fixed', self.tmp.name)

    def test_refresh_survives_restart_and_accounts_are_isolated(self):
        store = self.store()
        self.assertIsNone(store.load())
        self.assertTrue(store.save(self.state))
        self.assertEqual(self.store().load(), self.state)
        self.assertIsNone(self.store('b').load())

    def test_relogin_replaces_old_snapshot(self):
        store = self.store(); store.load(); store.save(self.state)
        relogin = self.store(cookies=[{**self.cookies[0], 'value':'new-login'}])
        self.assertIsNone(relogin.load())
        self.assertTrue(relogin.save(self.state))
        self.assertIsNone(self.store().load())

    def test_stale_parallel_writer_cannot_overwrite_new_snapshot(self):
        first = self.store(); second = self.store()
        first.load(); second.load()
        self.assertTrue(first.save(self.state))
        self.assertFalse(second.save({'cookies':self.cookies,'origins':[]}))
        self.assertEqual(self.store().load(), self.state)

    def test_atomic_failure_preserves_previous_state(self):
        store = self.store(); store.load(); store.save(self.state)
        with patch('core.session_store.os.replace', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                store.save({'cookies':self.cookies,'origins':[]})
        self.assertEqual(self.store().load(),self.state)
        self.assertEqual(list(Path(self.tmp.name).glob('.session-*')),[])

    def test_invalid_cookie_snapshot_is_not_saved(self):
        store = self.store(); store.load()
        self.assertFalse(store.save({'cookies':[],'origins':[]}))
        self.assertFalse(store.path.exists())

    def test_corrupt_file_falls_back_to_configured_cookies(self):
        store = self.store();store.directory.mkdir(exist_ok=True)
        store.path.write_text('{broken')
        self.assertIsNone(store.load())

    def test_cookie_sanitization_and_json_order_do_not_invalidate_state(self):
        store = self.store();store.load();store.save(self.state)
        same = self.store(cookies=[{**self.cookies[0],'sameSite':'Lax'}])
        self.assertEqual(same.load(),self.state)

    def test_browser_context_round_trip_retains_local_storage_and_indexeddb(self):
        from app.paths import browser_binary
        from playwright.sync_api import sync_playwright
        if not browser_binary().is_file():
            self.skipTest('No bundled browser')
        with sync_playwright() as pw, ExitStack() as cleanup:
            browser = pw.chromium.launch(executable_path=str(browser_binary()), headless=True)
            cleanup.callback(browser.close)
            context = browser.new_context()
            context.route('https://www.douyin.com/**', lambda route: route.fulfill(body='<html>Fixture</html>',content_type='text/html'))
            context.add_cookies(self.cookies)
            page = context.new_page(); page.goto('https://www.douyin.com/')
            page.evaluate("""async () => {
                localStorage.setItem('test-token','local');
                await new Promise((resolve,reject) => {
                    const r=indexedDB.open('fixture',1);
                    r.onupgradeneeded=()=>r.result.createObjectStore('tokens');
                    r.onerror=()=>reject(r.error);
                    r.onsuccess=()=>{const db=r.result;const tx=db.transaction('tokens','readwrite');tx.objectStore('tokens').put('indexed','token');tx.oncomplete=()=>{db.close();resolve();};};
                });
            }""")
            store=self.store();store.load();self.assertTrue(store.save(capture_state(context)))
            context.close()
            restored=browser.new_context(storage_state=self.store().load())
            restored.route('https://www.douyin.com/**', lambda route: route.fulfill(body='<html>Fixture</html>',content_type='text/html'))
            page=restored.new_page();page.goto('https://www.douyin.com/')
            self.assertEqual(page.evaluate("localStorage.getItem('test-token')"),'local')
            self.assertEqual(page.evaluate("""() => new Promise((resolve,reject) => {
                const r=indexedDB.open('fixture',1);r.onerror=()=>reject(r.error);
                r.onsuccess=()=>{const db=r.result;const tx=db.transaction('tokens','readonly');const get=tx.objectStore('tokens').get('token');get.onsuccess=()=>{resolve(get.result);db.close();};};
            })"""),'indexed')
            self.assertTrue(any(c['value']=='old' for c in restored.cookies() if c['name']=='sessionid'))
            restored.close()
