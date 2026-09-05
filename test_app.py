import tempfile
import io
import unittest
import warnings
import inspect
import json
import sqlite3
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import MagicMock, patch
from app import REMEMBER_COOKIE, REMEMBER_SECONDS, create_app, localtime, minute, parse_proxy_url, parse_random_range, parse_steps, utcnow
from vendor.util import zepp_helper

class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False); self.tmp.close()
        self.app = create_app({'DB': self.tmp.name, 'APP_SECRET': 'x'*40, 'ADMIN_PASSWORD': 'pass', 'COOKIE_SECURE': False, 'START_WORKER': False}).test_client()
    def login(self): return self.app.post('/login', data={'password':'pass'}, follow_redirects=True)
    def test_health_login_and_csrf(self):
        self.assertEqual(self.app.get('/healthz').status_code, 200)
        self.assertEqual(self.app.get('/').status_code, 302)
        self.login()
        self.assertEqual(self.app.post('/accounts', data={'username':'a@b.com','password':'p'}).status_code, 400)
        page=self.app.get('/accounts').data.decode(); self.assertIn("action='/accounts/import'><input type=hidden name=csrf", page); csrf=page.split("name=csrf value='")[1].split("'")[0]
        self.assertEqual(self.app.post('/accounts', data={'csrf':csrf,'username':'a@b.com','password':'p','note':'n'}).status_code, 302)
        with self.app.application.extensions['store'].conn() as c: self.assertNotIn('"password": "p"', c.execute('SELECT secret FROM accounts').fetchone()[0])
    def test_targets(self):
        self.assertEqual(minute('08:30'), 510); self.assertEqual(parse_steps('3'), 3)
        self.assertEqual(parse_random_range('-5', '3'), (-5, 3))
        self.assertEqual(parse_proxy_url('socks5h://user:pass@127.0.0.1:1080'), 'socks5h://user:pass@127.0.0.1:1080')
        with self.assertRaises(ValueError): parse_steps('0')
        with self.assertRaises(ValueError): parse_random_range('5', '3')
        with self.assertRaises(ValueError): parse_proxy_url('http://127.0.0.1:1080')
        for name in ('login_access_token', 'grant_login_tokens', 'grant_app_token', 'check_app_token', 'renew_login_token', 'get_user_device_id', 'post_fake_brand_data'):
            self.assertIn('client', inspect.signature(getattr(zepp_helper, name)).parameters)
    def test_encrypted_backup_restores_with_a_new_app_secret(self):
        self.login(); csrf = self.csrf()
        app, store = self.app.application, self.app.application.extensions['store']
        with store.conn() as c:
            c.execute("INSERT INTO plans(name,created_at) VALUES('backup',?)", (utcnow(),)); pid = c.execute('SELECT id FROM plans').fetchone()[0]
            c.execute('INSERT INTO plan_points(plan_id,at_minute,low_steps,high_steps) VALUES(?,?,?,?)', (pid, 600, 3000, 3000))
            point = c.execute('SELECT id FROM plan_points').fetchone()[0]
            c.execute("INSERT INTO accounts(username,secret,token_secret,plan_id,created_at,updated_at) VALUES(?,?,?,?,?,?)", ('backup@example.com', store.seal({'username':'backup@example.com','password':'secret'}), store.seal({'app_token':'token'}), pid, utcnow(), utcnow())); aid = c.execute('SELECT id FROM accounts').fetchone()[0]
            c.execute("INSERT INTO tasks(account_id,account_label,point_id,run_date,trigger,state,available_at,created_at) VALUES(?,?,?,'2026-09-05','计划','queued',?,?)", (aid, 'backup', point, utcnow(), utcnow()))
        store.save_proxy('socks5h://proxy.example:1080')
        exported = self.app.post('/settings/backup/export', data={'csrf':csrf, 'password':'correct horse battery', 'confirmation':'correct horse battery'})
        self.assertEqual(exported.status_code, 200); self.assertTrue(exported.data.startswith(b'STEPSBAK1'))
        target = tempfile.NamedTemporaryFile(suffix='.db', delete=False); target.close()
        restored = create_app({'DB':target.name, 'APP_SECRET':'y'*40, 'ADMIN_PASSWORD':'pass', 'COOKIE_SECURE':False, 'START_WORKER':False}).test_client()
        restored.post('/login', data={'password':'pass'})
        with restored.session_transaction() as session: target_csrf = session['csrf']
        response = restored.post('/settings/backup/import', data={'csrf':target_csrf, 'password':'correct horse battery', 'confirm':'overwrite', 'backup':(io.BytesIO(exported.data), 'backup.stepsbak')}, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn('恢复成功', response.data.decode())
        target_store = restored.application.extensions['store']
        with target_store.conn() as c:
            account = c.execute('SELECT * FROM accounts').fetchone()
            self.assertEqual(account['enabled'], 0); self.assertEqual(c.execute('SELECT state FROM tasks').fetchone()[0], 'skipped')
        self.assertEqual(target_store.open(account['secret'])['password'], 'secret')
        self.assertEqual(target_store.proxy_url(), 'socks5h://proxy.example:1080')
    def test_invalid_backup_does_not_change_current_data(self):
        store = self.app.application.extensions['store']
        with store.conn() as c:
            c.execute("INSERT INTO accounts(username,secret,created_at,updated_at) VALUES(?,?,?,?)", ('current@example.com', store.seal({'username':'current@example.com','password':'p'}), utcnow(), utcnow()))
        backup = store.backup('correct horse battery', 'Asia/Shanghai')
        with self.assertRaises(ValueError): store.restore(backup[:-1], 'correct horse battery')
        with self.assertRaises(ValueError): store.restore(backup, 'wrong password')
        with store.conn() as c: self.assertEqual(c.execute('SELECT username FROM accounts').fetchone()[0], 'current@example.com')
    def csrf(self):
        with self.app.session_transaction() as session: return session['csrf']
    def test_utcnow_is_warning_free(self):
        with warnings.catch_warnings():
            warnings.simplefilter('error', DeprecationWarning)
            self.assertRegex(utcnow(), r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$')
        self.assertEqual(localtime('2026-09-02T00:00:00', 'Asia/Shanghai'), '2026-09-02 08:00:00')
    def test_plan_monotonicity_and_schedule_dedupe(self):
        self.login()
        token = self.app.get('/plans').data.decode().split("name=csrf value='")[1].split("'")[0]
        self.app.post('/plans', data={'csrf':token, 'name':'每日计划'})
        app = self.app.application
        with app.extensions['store'].conn() as c: pid = c.execute('SELECT id FROM plans').fetchone()[0]
        first = self.app.post(f'/plans/{pid}', data={'csrf':token, 'at':'00:00', 'steps':'3000'}, follow_redirects=True)
        self.assertIn('时间点已添加', first.data.decode())
        second = self.app.post(f'/plans/{pid}', data={'csrf':token, 'at':'06:00', 'steps':'4000'}, follow_redirects=True)
        self.assertIn('时间点已添加', second.data.decode())
        detail = self.app.get(f'/plans/{pid}')
        self.assertEqual(detail.status_code, 200)
        self.assertIn('固定步数', detail.data.decode())
        self.app.post(f'/plans/{pid}', data={'csrf':token, 'at':'12:00', 'steps':'2000'})
        with app.extensions['store'].conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM plan_points').fetchone()[0], 2)
            c.execute("INSERT INTO accounts(username,note,secret,plan_id,created_at,updated_at) VALUES(?,?,?,?,?,?)", ('one@x.com','',app.extensions['store'].seal({'username':'one@x.com','password':'p'}),pid,'now','now'))
        app.extensions['runner'].enqueue_scheduled(); app.extensions['runner'].enqueue_scheduled()
        with app.extensions['store'].conn() as c: self.assertEqual(c.execute("SELECT count(*) FROM tasks WHERE trigger='计划'").fetchone()[0], 1)

    def test_plan_can_be_deleted_from_list(self):
        self.login()
        page = self.app.get('/plans').data.decode(); token = page.split("name=csrf value='")[1].split("'")[0]
        self.app.post('/plans', data={'csrf':token, 'name':'待删除'})
        app = self.app.application
        with app.extensions['store'].conn() as c: pid = c.execute('SELECT id FROM plans').fetchone()[0]
        page = self.app.get('/plans').data.decode()
        self.assertIn(f"action='/plans/{pid}/delete'", page)
        self.app.post(f'/plans/{pid}/delete', data={'csrf':token})
        with app.extensions['store'].conn() as c: self.assertEqual(c.execute('SELECT count(*) FROM plans').fetchone()[0], 0)

    def test_history_uses_configured_timezone(self):
        self.login(); app = self.app.application
        with app.extensions['store'].conn() as c:
            c.execute("INSERT INTO tasks(account_label,trigger,state,available_at,created_at) VALUES('***','测试','success',?,?)", ('2026-09-02T00:00:00', '2026-09-02T00:00:00'))
        page = self.app.get('/history').data.decode()
        self.assertIn('时间（Asia/Shanghai）', page)
        self.assertIn('2026-09-02 08:00:00', page)

    def test_bulk_plan_assignment_and_unassignment(self):
        self.login(); app = self.app.application
        with app.extensions['store'].conn() as c:
            c.execute("INSERT INTO plans(name,created_at) VALUES('bulk-plan',?)", (utcnow(),)); pid = c.execute('SELECT id FROM plans').fetchone()[0]
            for name in ('first@example.com', 'second@example.com'):
                c.execute("INSERT INTO accounts(username,secret,created_at,updated_at) VALUES(?,?,?,?)", (name, app.extensions['store'].seal({'username': name, 'password': 'p'}), utcnow(), utcnow()))
            ids = [row[0] for row in c.execute('SELECT id FROM accounts ORDER BY id')]
        page = self.app.get('/accounts').data.decode(); csrf = page.split("name=csrf value='")[1].split("'")[0]
        self.app.post('/accounts/plan', data={'csrf': csrf, 'account_id': ids[0], 'plan_id': pid})
        with app.extensions['store'].conn() as c:
            self.assertEqual(c.execute('SELECT plan_id FROM accounts WHERE id=?', (ids[0],)).fetchone()[0], pid)
            self.assertIsNone(c.execute('SELECT plan_id FROM accounts WHERE id=?', (ids[1],)).fetchone()[0])
        self.app.post('/accounts/plan', data={'csrf': csrf, 'account_id': ids[0], 'plan_id': ''})
        with app.extensions['store'].conn() as c: self.assertIsNone(c.execute('SELECT plan_id FROM accounts WHERE id=?', (ids[0],)).fetchone()[0])
        self.app.post('/accounts/plan', data={'csrf': csrf, 'account_id': ids[0], 'plan_id': '999999'})
        self.app.post('/accounts/plan', data={'csrf': csrf, 'plan_id': pid})
        with app.extensions['store'].conn() as c: self.assertIsNone(c.execute('SELECT plan_id FROM accounts WHERE id=?', (ids[0],)).fetchone()[0])

    def test_history_filters_and_prunes_all_old_task_states(self):
        self.login(); app = self.app.application
        with app.extensions['store'].conn() as c:
            for name in ('first@example.com', 'second@example.com'):
                c.execute("INSERT INTO accounts(username,secret,created_at,updated_at) VALUES(?,?,?,?)", (name, app.extensions['store'].seal({'username': name, 'password': 'p'}), utcnow(), utcnow()))
            first, second = [row[0] for row in c.execute('SELECT id FROM accounts ORDER BY id')]
            for state in ('queued', 'running', 'success', 'failed'):
                c.execute("INSERT INTO tasks(account_id,account_label,trigger,state,available_at,created_at) VALUES(?,?,?,?,?,?)", (first, 'old-' + state, 'test', state, utcnow(), utcnow(-8 * 24 * 60 * 60)))
            c.execute("INSERT INTO tasks(account_id,account_label,trigger,state,available_at,created_at) VALUES(?,?,?,?,?,?)", (first, 'first-recent', 'test', 'success', utcnow(), utcnow()))
            c.execute("INSERT INTO tasks(account_id,account_label,trigger,state,available_at,created_at) VALUES(?,?,?,?,?,?)", (second, 'second-recent', 'test', 'failed', utcnow(), utcnow()))
        self.assertEqual(app.extensions['store'].prune_tasks(), 4)
        page = self.app.get(f'/history?account_id={first}').data.decode()
        self.assertIn('first-recent', page); self.assertNotIn('second-recent', page); self.assertNotIn('old-queued', page)
        page = self.app.get('/history').data.decode()
        self.assertIn('first-recent', page); self.assertIn('second-recent', page)

    def test_responsive_account_and_history_markup(self):
        self.login()
        accounts = self.app.get('/accounts').data.decode(); history = self.app.get('/history').data.decode()
        self.assertIn('/static/app.css', accounts)
        self.assertIn('@media(max-width:700px)', self.app.get('/static/app.css').data.decode())
        self.assertIn('id=select-all', accounts)
        self.assertIn('class=responsive', accounts)
        self.assertIn('name=account_id', history)
        self.assertIn('7 天', history)

    def test_settings_encrypt_proxy_and_validate_exit_ip(self):
        self.login(); app = self.app.application
        page = self.app.get('/settings').data.decode(); csrf = page.split("name=csrf value='")[1].split("'")[0]
        proxy = 'socks5h://user:secret@127.0.0.1:1080'
        self.app.post('/settings/proxy', data={'csrf': csrf, 'proxy_url': proxy})
        with app.extensions['store'].conn() as c:
            secret = c.execute('SELECT proxy_secret FROM settings').fetchone()[0]
        self.assertNotIn('secret', secret)
        self.assertEqual(app.extensions['store'].proxy_url(), proxy)
        self.assertIn('socks5h://***@127.0.0.1:1080', self.app.get('/settings').data.decode())
        response = MagicMock(); response.json.return_value = {'ip': '203.0.113.4'}
        with patch('app.requests.Session') as session:
            session.return_value.get.return_value = response
            result = self.app.post('/settings/proxy/test', data={'csrf': csrf}, follow_redirects=True)
        self.assertIn('出口 IP：203.0.113.4', result.data.decode())
        self.assertFalse(session.return_value.trust_env)
        self.assertEqual(session.return_value.proxies.update.call_args.args[0]['https'], proxy)

    def test_random_tasks_are_per_account_and_retry_keeps_target(self):
        self.login(); app = self.app.application; store = app.extensions['store']; runner = app.extensions['runner']
        with store.conn() as c:
            for name in ('one@example.com', 'two@example.com'):
                c.execute("INSERT INTO accounts(username,secret,created_at,updated_at) VALUES(?,?,?,?)", (name, store.seal({'username': name, 'password': 'p'}), utcnow(), utcnow()))
            ids = [row[0] for row in c.execute('SELECT id FROM accounts ORDER BY id')]
        store.save_random(True, -2, 2)
        with patch('app.random.randint', side_effect=[-2, 2]):
            self.assertEqual(runner.enqueue(ids, '100'), (2, 0))
        with store.conn() as c:
            rows = c.execute("SELECT target_steps FROM tasks WHERE trigger='手动' ORDER BY id").fetchall()
            tid = c.execute("SELECT id FROM tasks WHERE target_steps=98").fetchone()[0]
        self.assertEqual([row[0] for row in rows], [98, 102])
        page = self.app.get('/history').data.decode(); csrf = page.split("name=csrf value='")[1].split("'")[0]
        with store.conn() as c: c.execute("UPDATE tasks SET state='failed' WHERE id=?", (tid,))
        self.app.post(f'/tasks/{tid}/retry', data={'csrf': csrf})
        with store.conn() as c: self.assertEqual(c.execute('SELECT target_steps FROM tasks WHERE id=?', (tid,)).fetchone()[0], 98)

    def test_range_migration_and_lower_target_skip(self):
        app = self.app.application; store = app.extensions['store']; runner = app.extensions['runner']
        with store.conn() as c:
            c.execute("INSERT INTO plans(name,created_at) VALUES('old',?)", (utcnow(),)); pid = c.execute('SELECT id FROM plans').fetchone()[0]
            c.execute("INSERT INTO plan_points(plan_id,at_minute,low_steps,high_steps) VALUES(?,?,?,?)", (pid, 0, 3000, 5000))
            c.execute("INSERT INTO accounts(username,secret,created_at,updated_at) VALUES(?,?,?,?)", ('one@example.com', store.seal({'username': 'one@example.com', 'password': 'p'}), utcnow(), utcnow()))
            aid = c.execute('SELECT id FROM accounts').fetchone()[0]
            c.execute("INSERT INTO tasks(account_id,account_label,run_date,trigger,target_steps,state,available_at,created_at) VALUES(?,?,?,?,?,?,?,?)", (aid, 'one***com', datetime.now(runner.tz).date().isoformat(), '手动', 100, 'success', utcnow(), utcnow()))
        store.init()
        with store.conn() as c: self.assertEqual(tuple(c.execute('SELECT low_steps,high_steps FROM plan_points').fetchone()), (3000, 3000))
        self.assertEqual(runner.enqueue([aid], '50'), (0, 1))
        with store.conn() as c:
            skipped = c.execute("SELECT state,error FROM tasks WHERE target_steps=50").fetchone()
        self.assertEqual(skipped[0], 'skipped'); self.assertIn('低于当天已成功步数', skipped[1])

    def test_plan_time_random_settings_and_cross_day_validation(self):
        self.login(); app = self.app.application; store = app.extensions['store']
        with store.conn() as c:
            c.execute("INSERT INTO plans(name,created_at) VALUES('time-random',?)", (utcnow(),)); pid = c.execute('SELECT id FROM plans').fetchone()[0]
            c.execute('INSERT INTO plan_points(plan_id,at_minute,low_steps,high_steps) VALUES(?,?,?,?)', (pid, 10, 3000, 3000))
            plan = c.execute('SELECT time_random_enabled,time_random_min,time_random_max FROM plans WHERE id=?', (pid,)).fetchone()
        self.assertEqual(tuple(plan), (0, -10, 10))
        csrf = self.app.get(f'/plans/{pid}').data.decode().split("name=csrf value='")[1].split("'")[0]
        self.app.post(f'/plans/{pid}/time-random', data={'csrf': csrf, 'enabled': '1', 'low': '-10', 'high': '10'})
        with store.conn() as c: self.assertEqual(tuple(c.execute('SELECT time_random_enabled,time_random_min,time_random_max FROM plans WHERE id=?', (pid,)).fetchone()), (1, -10, 10))
        self.app.post(f'/plans/{pid}', data={'csrf': csrf, 'at': '00:00', 'steps': '4000'})
        with store.conn() as c: self.assertEqual(c.execute('SELECT count(*) FROM plan_points WHERE plan_id=?', (pid,)).fetchone()[0], 1)

    def test_legacy_plan_table_migrates_time_random_columns(self):
        legacy = tempfile.NamedTemporaryFile(suffix='.db', delete=False); legacy.close()
        with sqlite3.connect(legacy.name) as c:
            c.execute('CREATE TABLE plans (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)')
            c.execute("INSERT INTO plans(name,created_at) VALUES('legacy','now')")
        legacy_app = create_app({'DB': legacy.name, 'APP_SECRET': 'x'*40, 'ADMIN_PASSWORD': 'pass', 'COOKIE_SECURE': False, 'START_WORKER': False})
        with legacy_app.extensions['store'].conn() as c:
            columns = {row['name'] for row in c.execute('PRAGMA table_info(plans)')}
            self.assertTrue({'time_random_enabled', 'time_random_min', 'time_random_max'} <= columns)
            self.assertEqual(tuple(c.execute('SELECT time_random_enabled,time_random_min,time_random_max FROM plans').fetchone()), (0, -10, 10))

    def test_daily_time_offsets_are_per_account_and_stable(self):
        app = self.app.application; store = app.extensions['store']; runner = app.extensions['runner']
        with store.conn() as c:
            c.execute("INSERT INTO plans(name,time_random_enabled,time_random_min,time_random_max,created_at) VALUES('daily',1,-10,10,?)", (utcnow(),)); pid = c.execute('SELECT id FROM plans').fetchone()[0]
            for at, steps in ((600, 3000), (620, 4000)): c.execute('INSERT INTO plan_points(plan_id,at_minute,low_steps,high_steps) VALUES(?,?,?,?)', (pid, at, steps, steps))
            for name in ('one@example.com', 'two@example.com'): c.execute("INSERT INTO accounts(username,secret,plan_id,created_at,updated_at) VALUES(?,?,?,?,?)", (name, store.seal({'username': name, 'password': 'p'}), pid, utcnow(), utcnow()))
            point_ids = [row[0] for row in c.execute('SELECT id FROM plan_points ORDER BY at_minute')]
        now = datetime(2026, 9, 2, 9, 55, tzinfo=runner.tz)
        with patch.object(runner, 'now', return_value=now), patch('app.random.randint', side_effect=[-10, 5, 0, 10]): runner.enqueue_scheduled()
        with store.conn() as c:
            snapshots = c.execute('SELECT offsets_json FROM daily_plan_offsets ORDER BY account_id').fetchall()
            self.assertEqual(json.loads(snapshots[0][0]), {str(point_ids[0]): -10, str(point_ids[1]): 5})
            self.assertEqual(json.loads(snapshots[1][0]), {str(point_ids[0]): 0, str(point_ids[1]): 10})
            self.assertEqual(c.execute("SELECT count(*) FROM tasks WHERE trigger='计划'").fetchone()[0], 1)
        with patch.object(runner, 'now', return_value=now), patch('app.random.randint', side_effect=AssertionError('must not redraw')): runner.enqueue_scheduled()
        tomorrow = datetime(2026, 9, 3, 9, 0, tzinfo=runner.tz)
        with patch.object(runner, 'now', return_value=tomorrow), patch('app.random.randint', side_effect=[1, 2, 3, 4]): runner.enqueue_scheduled()
        with store.conn() as c: self.assertEqual(c.execute('SELECT count(*) FROM daily_plan_offsets').fetchone()[0], 2)

    def test_today_plan_view_creates_snapshot_used_by_scheduler(self):
        self.login(); app = self.app.application; store = app.extensions['store']; runner = app.extensions['runner']
        with store.conn() as c:
            c.execute("INSERT INTO plans(name,time_random_enabled,time_random_min,time_random_max,created_at) VALUES('view',1,-10,10,?)", (utcnow(),)); pid = c.execute('SELECT id FROM plans').fetchone()[0]
            c.execute('INSERT INTO plan_points(plan_id,at_minute,low_steps,high_steps) VALUES(?,?,?,?)', (pid, 600, 3000, 3000))
            c.execute("INSERT INTO accounts(username,secret,plan_id,created_at,updated_at) VALUES(?,?,?,?,?)", ('view@example.com', store.seal({'username': 'view@example.com', 'password': 'p'}), pid, utcnow(), utcnow())); aid = c.execute('SELECT id FROM accounts').fetchone()[0]
        now = datetime(2026, 9, 2, 9, 55, tzinfo=runner.tz)
        with patch.object(runner, 'now', return_value=now), patch('app.random.randint', return_value=-5): page = self.app.get(f'/accounts/{aid}/today').data.decode()
        self.assertIn('09:55', page); self.assertIn('-5', page); self.assertIn('待调度', page)
        with patch.object(runner, 'now', return_value=now), patch('app.random.randint', side_effect=AssertionError('must reuse view snapshot')): runner.enqueue_scheduled()
        with store.conn() as c: self.assertEqual(c.execute("SELECT count(*) FROM tasks WHERE account_id=? AND trigger='计划'", (aid,)).fetchone()[0], 1)

    def test_time_offset_order_reversal_uses_final_time_and_skips_lower_target(self):
        app = self.app.application; store = app.extensions['store']; runner = app.extensions['runner']
        with store.conn() as c:
            c.execute("INSERT INTO plans(name,time_random_enabled,time_random_min,time_random_max,created_at) VALUES('reverse',1,-10,10,?)", (utcnow(),)); pid = c.execute('SELECT id FROM plans').fetchone()[0]
            for at, steps in ((600, 100), (605, 200)): c.execute('INSERT INTO plan_points(plan_id,at_minute,low_steps,high_steps) VALUES(?,?,?,?)', (pid, at, steps, steps))
            c.execute("INSERT INTO accounts(username,secret,plan_id,created_at,updated_at) VALUES(?,?,?,?,?)", ('reverse@example.com', store.seal({'username': 'reverse@example.com', 'password': 'p'}), pid, utcnow(), utcnow())); aid = c.execute('SELECT id FROM accounts').fetchone()[0]
        first = datetime(2026, 9, 2, 10, 0, tzinfo=runner.tz)
        with patch.object(runner, 'now', return_value=first), patch('app.random.randint', side_effect=[10, -10]): runner.enqueue_scheduled()
        with store.conn() as c:
            task = c.execute("SELECT id,target_steps FROM tasks WHERE account_id=?", (aid,)).fetchone()
            self.assertEqual(task['target_steps'], 200); c.execute("UPDATE tasks SET state='success' WHERE id=?", (task['id'],))
        later = datetime(2026, 9, 2, 10, 10, tzinfo=runner.tz)
        with patch.object(runner, 'now', return_value=later): runner.enqueue_scheduled()
        with store.conn() as c: self.assertEqual(c.execute('SELECT state FROM tasks WHERE account_id=? AND target_steps=100', (aid,)).fetchone()[0], 'skipped')

class DashboardAndAuthTests(unittest.TestCase):
    setUp = AppTests.setUp

    def remember(self, client=None):
        client = client or self.app
        response = client.post('/login', data={'password': 'pass', 'remember': '1'})
        return client.get_cookie(REMEMBER_COOKIE).value, response

    def csrf(self, client=None):
        with (client or self.app).session_transaction() as session: return session['csrf']

    def test_normal_and_remember_cookie_security(self):
        self.app.post('/login', data={'password': 'pass'})
        self.assertIsNone(self.app.get_cookie(REMEMBER_COOKIE))
        self.assertIsNone(self.app.get_cookie('session').expires)
        token, response = self.remember()
        cookie = self.app.get_cookie(REMEMBER_COOKIE)
        self.assertTrue(cookie.http_only); self.assertEqual(cookie.same_site, 'Lax')
        self.assertEqual(cookie.max_age, REMEMBER_SECONDS)
        with self.app.application.extensions['store'].conn() as c:
            row = c.execute('SELECT * FROM admin_credentials').fetchone()
            self.assertNotEqual(row['token_hash'], token)
            self.assertEqual(row['token_hash'], hashlib.sha256(token.encode()).hexdigest())
        self.app.application.config['COOKIE_SECURE'] = True
        _, response = self.remember()
        self.assertTrue(any('Secure' in value for value in response.headers.getlist('Set-Cookie') if value.startswith(REMEMBER_COOKIE)))
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_restore_rotates_extends_and_rejects_replay(self):
        token, _ = self.remember(); old_csrf = self.csrf()
        self.app.delete_cookie('session')
        next_login, new_expiry = utcnow(86400), utcnow(86400 + REMEMBER_SECONDS)
        with patch('app.utcnow', side_effect=lambda delay=0: new_expiry if delay else next_login):
            response = self.app.get('/')
        self.assertEqual(response.status_code, 200)
        new_token = self.app.get_cookie(REMEMBER_COOKIE).value
        self.assertNotEqual(new_token, token); self.assertNotEqual(self.csrf(), old_csrf)
        with self.app.application.extensions['store'].conn() as c:
            self.assertEqual(c.execute('SELECT expires_at FROM admin_credentials').fetchone()[0], new_expiry)
        self.app.get('/')
        self.assertEqual(self.app.get_cookie(REMEMBER_COOKIE).value, new_token)
        replay = self.app.application.test_client(); replay.set_cookie(REMEMBER_COOKIE, token)
        response = replay.get('/dashboard/timeline')
        self.assertEqual(response.status_code, 302)
        self.assertFalse(any(value.startswith(REMEMBER_COOKIE) for value in response.headers.getlist('Set-Cookie')))
        self.assertEqual(self.app.post('/run', data={'csrf': old_csrf}).status_code, 400)

    def test_expired_forged_and_password_change(self):
        self.remember()
        with self.app.application.extensions['store'].conn() as c: c.execute("UPDATE admin_credentials SET expires_at='2000-01-01T00:00:00'")
        self.app.delete_cookie('session'); self.assertEqual(self.app.get('/').status_code, 302)
        self.app.set_cookie(REMEMBER_COOKIE, 'forged'); self.assertEqual(self.app.get('/').status_code, 302)
        self.remember(); self.app.application.config['ADMIN_PASSWORD'] = 'changed'
        self.assertEqual(self.app.get('/').status_code, 302)
        self.app.delete_cookie('session'); self.assertEqual(self.app.get('/').status_code, 302)
        with self.app.application.extensions['store'].conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM admin_credentials').fetchone()[0], 0)

    def test_logout_and_password_login_revoke_only_current_browser(self):
        self.assertEqual(self.app.post('/logout').status_code, 400)
        old, _ = self.remember(); other = self.app.application.test_client(); other_token, _ = self.remember(other)
        csrf = self.csrf()
        self.assertEqual(self.app.post('/logout', data={'csrf': csrf}).status_code, 302)
        self.assertIsNone(self.app.get_cookie(REMEMBER_COOKIE))
        with self.app.application.extensions['store'].conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM admin_credentials').fetchone()[0], 1)
        other.delete_cookie('session'); self.assertEqual(other.get('/').status_code, 200)
        self.remember(); self.app.post('/login', data={'password': 'pass'})
        self.assertIsNone(self.app.get_cookie(REMEMBER_COOKIE))
        with self.app.application.extensions['store'].conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM admin_credentials').fetchone()[0], 1)

    def test_concurrent_token_redemption_has_one_winner(self):
        token, _ = self.remember()
        def restore(_):
            client = self.app.application.test_client(); client.set_cookie(REMEMBER_COOKIE, token)
            response = client.get('/dashboard/timeline')
            return response.status_code, response.headers.getlist('Set-Cookie')
        with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(restore, range(2)))
        self.assertEqual(sorted(status for status, _ in results), [200, 302])
        for status, cookies in results:
            if status == 302: self.assertFalse(any(cookie.startswith(REMEMBER_COOKIE) for cookie in cookies))
        with self.app.application.extensions['store'].conn() as c: self.assertEqual(c.execute('SELECT count(*) FROM admin_credentials').fetchone()[0], 1)

    def seed_timeline(self):
        self.app.post('/login', data={'password': 'pass'})
        store = self.app.application.extensions['store']; runner = self.app.application.extensions['runner']
        with store.conn() as c:
            for name in ('A plan', 'B disabled'):
                c.execute('INSERT INTO plans(name,enabled,created_at) VALUES(?,?,?)', (name, name == 'A plan', utcnow()))
            for at in (0, 120, 240, 360, 480, 600, 720, 1439):
                c.execute('INSERT INTO plan_points(plan_id,at_minute,low_steps,high_steps) VALUES(1,?,?,?)', (at, 100 + at, 100 + at))
            c.execute('INSERT INTO plan_points(plan_id,at_minute,low_steps,high_steps) VALUES(2,600,100,100)')
            for index, plan_id in enumerate((1, 1, 2, None), 1):
                c.execute('INSERT INTO accounts(username,note,secret,plan_id,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?)', (f'account{index}@example.com', '<script>unsafe</script> {{7*7}} 备注', 'never-return-this-secret', plan_id, index != 2, utcnow(), utcnow()))
            for point_id, state, attempts in ((1, 'success', 1), (2, 'failed', 3), (3, 'queued', 1), (4, 'running', 1), (5, 'skipped', 0)):
                c.execute("INSERT INTO tasks(account_id,account_label,point_id,run_date,trigger,state,attempts,available_at,created_at) VALUES(1,'a',?,'2026-09-05','计划',?,?,?,?)", (point_id, state, attempts, utcnow(), utcnow()))
            for trigger in ('手动', '测试'):
                c.execute("INSERT INTO tasks(account_id,account_label,run_date,trigger,available_at,created_at) VALUES(1,'a','2026-09-05',?,?,?)", (trigger, utcnow(), utcnow()))
        return runner, datetime(2026, 9, 5, 10, 0, tzinfo=runner.tz)

    def test_timeline_auth_empty_filter_colors_and_safe_payload(self):
        self.assertEqual(self.app.get('/dashboard/timeline').status_code, 302)
        self.app.post('/login', data={'password': 'pass'})
        self.assertEqual(self.app.get('/dashboard/timeline').json['points'], [])
        runner, now = self.seed_timeline()
        with patch.object(runner, 'now', return_value=now):
            response = self.app.get('/dashboard/timeline'); payload = response.json
            self.assertEqual(payload['date'], '2026-09-05'); self.assertEqual(payload['timezone'], 'Asia/Shanghai')
            self.assertEqual(len(payload['points']), 17)
            rows = payload['points'][:8]
            self.assertEqual([row['color'] for row in rows], ['green', 'red'] + ['gray'] * 6)
            self.assertEqual([row['status'] for row in rows], ['成功', '失败', '重试等待', '执行中', '已跳过', '待调度', '待执行', '待执行'])
            self.assertTrue(all(row['status'] == '不会自动执行' for row in payload['points'][8:]))
            self.assertEqual(len(self.app.get('/dashboard/timeline?plan_id=2').json['points']), 1)
            self.assertEqual(self.app.get('/dashboard/timeline?plan_id=999').status_code, 404)
            self.assertEqual(self.app.get('/dashboard/timeline?plan_id=bad').status_code, 400)
            self.assertEqual(rows[0]['final_minute'], 0); self.assertEqual(rows[-1]['final_minute'], 1439)
            page = self.app.get('/').data.decode()
            self.assertNotIn('<script>unsafe</script>', page)
            self.assertNotIn('never-return-this-secret', response.data.decode())
            self.assertNotIn('account1@example.com', response.data.decode())
            self.assertEqual(rows[0]['note'], '<script>unsafe</script> {{7*7}} 备注')

    def test_timeline_snapshot_reuse_and_next_day_changes(self):
        runner, now = self.seed_timeline(); store = self.app.application.extensions['store']
        with store.conn() as c:
            c.execute('DELETE FROM tasks'); c.execute('DELETE FROM plan_points WHERE at_minute IN (0,1439)')
            c.execute('UPDATE plans SET time_random_enabled=1,time_random_min=-5,time_random_max=5 WHERE id=1')
        with patch.object(runner, 'now', return_value=now), patch('app.random.randint', return_value=3):
            first = self.app.get('/dashboard/timeline?plan_id=1').json
        with patch.object(runner, 'now', return_value=now), patch('app.random.randint', side_effect=AssertionError('must reuse snapshot')):
            self.assertEqual(first, self.app.get('/dashboard/timeline?plan_id=1').json)
            page = self.app.get('/accounts/1/today').data.decode()
            self.assertIn('02:03', page)
            runner.enqueue_scheduled()
            with store.conn() as c:
                c.execute('INSERT INTO plan_points(plan_id,at_minute,low_steps,high_steps) VALUES(1,800,1000,1000)')
            self.assertEqual(len(self.app.get('/dashboard/timeline?plan_id=1').json['points']), 12)
        tomorrow = datetime(2026, 9, 6, 0, 0, tzinfo=runner.tz)
        with patch.object(runner, 'now', return_value=tomorrow), patch('app.random.randint', return_value=-2):
            payload = self.app.get('/dashboard/timeline?plan_id=1').json
            self.assertEqual(payload['date'], '2026-09-06'); self.assertEqual(payload['now_minute'], 0)
            self.assertEqual(len(payload['points']), 14)
            self.assertEqual(payload['points'][0]['final_minute'], 118)
            self.assertNotEqual(payload['points'][0]['id'], first['points'][0]['id'])

    def test_missed_deleted_points_and_other_timezone(self):
        runner, now = self.seed_timeline(); store = self.app.application.extensions['store']
        from zoneinfo import ZoneInfo
        self.app.application.config['TZ'] = 'America/New_York'; runner.tz = ZoneInfo('America/New_York')
        midnight = datetime(2026, 9, 4, 23, 59, tzinfo=runner.tz)
        with patch.object(runner, 'now', return_value=midnight):
            payload = self.app.get('/dashboard/timeline').json
            self.assertEqual(payload['date'], '2026-09-04')
            self.assertEqual(payload['points'][0]['status'], '已错过')
            with store.conn() as c: c.execute('DELETE FROM plan_points WHERE id=1')
            self.assertEqual(len(self.app.get('/dashboard/timeline').json['points']), 15)

    def test_failed_login_does_not_issue_token_and_restart_preserves_valid_token(self):
        self.app.post('/login', data={'password': 'wrong', 'remember': '1'})
        self.assertIsNone(self.app.get_cookie(REMEMBER_COOKIE))
        token, _ = self.remember()
        config = {key: self.app.application.config[key] for key in ('DB', 'ADMIN_PASSWORD', 'START_WORKER', 'TZ', 'COOKIE_SECURE')}
        config['APP_SECRET'] = 'x' * 40
        restarted = create_app(config).test_client(); restarted.set_cookie(REMEMBER_COOKIE, token)
        self.assertEqual(restarted.get('/').status_code, 200)
        self.assertNotEqual(restarted.get_cookie(REMEMBER_COOKIE).value, token)

    def test_task_completion_changes_color_without_moving_point(self):
        runner, now = self.seed_timeline(); store = self.app.application.extensions['store']
        with patch.object(runner, 'now', return_value=now):
            first = self.app.get('/dashboard/timeline?plan_id=1').json['points'][2]
            with store.conn() as c:
                c.execute("UPDATE tasks SET state='success',started_at=?,finished_at=? WHERE point_id=3", (utcnow(), utcnow()))
            last = self.app.get('/dashboard/timeline?plan_id=1').json['points'][2]
        self.assertEqual(last['final_minute'], first['final_minute'])
        self.assertEqual(first['color'], 'gray'); self.assertEqual(last['color'], 'green')


if __name__ == '__main__': unittest.main()
