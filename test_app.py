import tempfile
import unittest
import warnings
from app import create_app, localtime, minute, parse_target, utcnow

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
        self.assertEqual(minute('08:30'), 510); self.assertEqual(parse_target('3','5'),(3,5))
        with self.assertRaises(ValueError): parse_target('5','3')
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
        self.app.post(f'/plans/{pid}', data={'csrf':token, 'at':'00:00', 'low':'3000'})
        self.app.post(f'/plans/{pid}', data={'csrf':token, 'at':'06:00', 'low':'4000', 'high':'5000'})
        detail = self.app.get(f'/plans/{pid}')
        self.assertEqual(detail.status_code, 200)
        self.assertIn('4000 – 5000', detail.data.decode())
        self.app.post(f'/plans/{pid}', data={'csrf':token, 'at':'12:00', 'low':'2000'})
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

if __name__ == '__main__': unittest.main()
