import tempfile
import unittest
import warnings
import inspect
from datetime import datetime
from unittest.mock import MagicMock, patch
from app import create_app, localtime, minute, parse_proxy_url, parse_random_range, parse_steps, utcnow
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
        self.assertIn('@media(max-width:700px)', accounts)
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

if __name__ == '__main__': unittest.main()
