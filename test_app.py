import tempfile
import unittest
import warnings
from app import create_app, minute, parse_target, utcnow

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
    def test_plan_monotonicity_and_schedule_dedupe(self):
        self.login()
        token = self.app.get('/plans').data.decode().split("name=csrf value='")[1].split("'")[0]
        self.app.post('/plans', data={'csrf':token, 'name':'每日计划'})
        app = self.app.application
        with app.extensions['store'].conn() as c: pid = c.execute('SELECT id FROM plans').fetchone()[0]
        self.app.post(f'/plans/{pid}', data={'csrf':token, 'at':'00:00', 'low':'3000'})
        self.app.post(f'/plans/{pid}', data={'csrf':token, 'at':'12:00', 'low':'2000'})
        with app.extensions['store'].conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM plan_points').fetchone()[0], 1)
            c.execute("INSERT INTO accounts(username,note,secret,plan_id,created_at,updated_at) VALUES(?,?,?,?,?,?)", ('one@x.com','',app.extensions['store'].seal({'username':'one@x.com','password':'p'}),pid,'now','now'))
        app.extensions['runner'].enqueue_scheduled(); app.extensions['runner'].enqueue_scheduled()
        with app.extensions['store'].conn() as c: self.assertEqual(c.execute("SELECT count(*) FROM tasks WHERE trigger='计划'").fetchone()[0], 1)

if __name__ == '__main__': unittest.main()
