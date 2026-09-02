"""Private Zepp Life step scheduler.  Protocol helper derives from mimotion."""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from Crypto.Cipher import AES
from flask import Flask, abort, flash, redirect, render_template_string, request, session, url_for
from vendor.util import zepp_helper

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS plans (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan_points (id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE, at_minute INTEGER NOT NULL, low_steps INTEGER NOT NULL, high_steps INTEGER NOT NULL, UNIQUE(plan_id, at_minute));
CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, note TEXT NOT NULL DEFAULT '', secret TEXT NOT NULL, token_secret TEXT, plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL, account_label TEXT NOT NULL, point_id INTEGER REFERENCES plan_points(id) ON DELETE SET NULL, run_date TEXT, trigger TEXT NOT NULL, target_steps INTEGER, state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT, created_at TEXT NOT NULL, UNIQUE(account_id, point_id, run_date));
CREATE INDEX IF NOT EXISTS task_queue ON tasks(state, available_at, id);
"""

LAYOUT = """<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>Zepp Life 步数计划</title><style>
body{font:15px system-ui,sans-serif;margin:auto;max-width:1100px;padding:18px;color:#172033;background:#f6f8fb}nav{display:flex;gap:14px;align-items:center;margin-bottom:18px}nav a{color:#0756b5;text-decoration:none}nav form{margin-left:auto}.card{background:#fff;padding:16px;margin:12px 0;border-radius:9px;box-shadow:0 1px 3px #0001}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #e5e7eb;text-align:left}input,select,textarea,button{padding:7px;margin:3px}textarea{width:100%;min-height:90px}button{cursor:pointer}.ok{color:#087443}.bad{color:#b42318}.muted{color:#667085}.flash{padding:10px;background:#fff2cc;border-radius:6px;margin:8px 0}.inline{display:inline} .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:8px}</style>
<nav><strong>Zepp Life 步数计划</strong><a href='{{url_for("dashboard")}}'>仪表盘</a><a href='{{url_for("accounts")}}'>账户</a><a href='{{url_for("plans")}}'>计划</a><a href='{{url_for("history")}}'>执行记录</a><form method=post action='{{url_for("logout")}}'><input type=hidden name=csrf value='{{csrf}}'><button>退出</button></form></nav>
{% with m=get_flashed_messages() %}{% for x in m %}<div class=flash>{{x}}</div>{% endfor %}{% endwith %}{{body|safe}}</html>"""

def utcnow(delay=0): return (datetime.now(UTC) + timedelta(seconds=delay)).replace(tzinfo=None, microsecond=0).isoformat()
def mask(value):
    value = str(value); n = max(1, len(value) // 3)
    return value[:n] + "***" + value[-n:]
def minute(value):
    h, m = map(int, value.split(":"));
    if not 0 <= h < 24 or not 0 <= m < 60: raise ValueError("时间必须是 HH:MM")
    return h * 60 + m
def clock(value): return f"{value // 60:02d}:{value % 60:02d}"
def parse_target(low, high):
    low, high = int(low), int(high or low)
    if low < 1 or high < low: raise ValueError("步数必须为正整数，且最大值不能小于最小值")
    return low, high

class Store:
    def __init__(self, path, key): self.path, self.key = str(path), key
    def conn(self):
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None); con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA foreign_keys=ON"); return con
    def init(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c: c.executescript(SCHEMA); c.execute("UPDATE tasks SET state='queued', started_at=NULL WHERE state='running'")
    def seal(self, value):
        cipher = AES.new(self.key, AES.MODE_GCM); data, tag = cipher.encrypt_and_digest(json.dumps(value).encode())
        return base64.urlsafe_b64encode(cipher.nonce + tag + data).decode()
    def open(self, value):
        raw = base64.urlsafe_b64decode(value); cipher = AES.new(self.key, AES.MODE_GCM, nonce=raw[:16])
        return json.loads(cipher.decrypt_and_verify(raw[32:], raw[16:32]).decode())

class Runner:
    def __init__(self, store, tz, delay): self.store, self.tz, self.delay = store, ZoneInfo(tz), delay
    def enqueue_scheduled(self):
        now = datetime.now(self.tz); date, now_min = now.date().isoformat(), now.hour * 60 + now.minute
        with self.store.conn() as c:
            rows = c.execute("""SELECT a.*, p.id point_id,p.at_minute,p.low_steps,p.high_steps FROM accounts a JOIN plans pl ON pl.id=a.plan_id AND pl.enabled=1 JOIN plan_points p ON p.plan_id=pl.id WHERE a.enabled=1 AND p.at_minute<=? ORDER BY a.id,p.at_minute""", (now_min,)).fetchall()
            latest = {}
            for r in rows: latest[r['id']] = r
            for r in latest.values():
                target = random.randint(r['low_steps'], r['high_steps'])
                c.execute("INSERT OR IGNORE INTO tasks(account_id,account_label,point_id,run_date,trigger,target_steps,available_at,created_at) VALUES(?,?,?,?,?,?,?,?)", (r['id'], mask(r['username']), r['point_id'], date, '计划', target, utcnow(), utcnow()))
    def enqueue(self, ids, low, high, trigger='手动'):
        low, high = parse_target(low, high); now = datetime.now(self.tz); date = now.date().isoformat(); queued = 0
        with self.store.conn() as c:
            for aid in ids:
                a = c.execute("SELECT * FROM accounts WHERE id=? AND enabled=1", (aid,)).fetchone()
                if not a: continue
                target = random.randint(low, high)
                previous = c.execute("SELECT MAX(target_steps) FROM tasks WHERE account_id=? AND run_date=? AND state='success'", (aid, date)).fetchone()[0]
                if previous is not None and target < previous: continue
                c.execute("INSERT INTO tasks(account_id,account_label,trigger,target_steps,available_at,created_at) VALUES(?,?,?,?,?,?)", (aid, mask(a['username']), trigger, target, utcnow(), utcnow())); queued += 1
        return queued
    def claim(self):
        with self.store.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM tasks WHERE state='queued' AND available_at<=? ORDER BY id LIMIT 1", (utcnow(),)).fetchone()
            if row: c.execute("UPDATE tasks SET state='running',attempts=attempts+1,started_at=? WHERE id=?", (utcnow(), row['id']))
            c.commit(); return row
    def credentials(self, account): return self.store.open(account['secret']), self.store.open(account['token_secret']) if account['token_secret'] else {}
    def authenticate(self, account):
        secret, tokens = self.credentials(account); user, password = secret['username'], secret['password']
        user = user if user.startswith('+86') or '@' in user else '+86' + user; phone = user.startswith('+86')
        device = tokens.get('device_id') or str(uuid.uuid4()); app_token = tokens.get('app_token'); uid = tokens.get('user_id')
        if app_token:
            try: ok, _ = zepp_helper.check_app_token(app_token)
            except Exception: ok = False
        else: ok = False
        if not ok and tokens.get('login_token'):
            app_token, _ = zepp_helper.grant_app_token(tokens['login_token']); ok = bool(app_token)
        if not ok and tokens.get('access_token'):
            login, app_token, uid, _ = zepp_helper.grant_login_tokens(tokens['access_token'], device, phone)
            if login: tokens['login_token'] = login; ok = True
        if not ok:
            access, err = zepp_helper.login_access_token(user, password)
            if not access: raise ValueError("认证失败：" + (err or "账号或密码不可用"))
            login, app_token, uid, err = zepp_helper.grant_login_tokens(access, device, phone)
            if not login: raise ValueError("认证失败：" + (err or "无法获取登录令牌"))
            tokens.update(access_token=access, login_token=login); ok = True
        tokens.update(app_token=app_token, user_id=uid, device_id=device)
        if not uid or not app_token: raise ValueError("认证失败：缺少有效身份信息")
        if not tokens.get('bound_device_id'):
            tokens['bound_device_id'] = zepp_helper.get_user_device_id(app_token, uid)
        with self.store.conn() as c: c.execute("UPDATE accounts SET token_secret=?,updated_at=? WHERE id=?", (self.store.seal(tokens), utcnow(), account['id']))
        return app_token, uid, tokens.get('bound_device_id')
    def execute(self, task):
        with self.store.conn() as c: account = c.execute("SELECT * FROM accounts WHERE id=?", (task['account_id'],)).fetchone()
        if not account: return False, "账户已删除", False
        try:
            app_token, uid, device = self.authenticate(account)
            if task['trigger'] == '测试': return True, "登录和设备检查成功", False
            ok, message = zepp_helper.post_fake_brand_data(str(task['target_steps']), app_token, uid, device)
            return ok, ("提交成功" if ok else "提交失败：" + str(message)), not ok and any(x in str(message) for x in ('429', '500', '502', '503', '504'))
        except Exception as exc:
            message = str(exc)[:300]; return False, message, not message.startswith('认证失败')
    def work_once(self):
        self.enqueue_scheduled(); task = self.claim()
        if not task: return False
        ok, msg, retryable = self.execute(task); attempt = task['attempts'] + 1
        with self.store.conn() as c:
            if not ok and retryable and attempt < 3:
                wait = 60 if attempt == 1 else 300
                c.execute("UPDATE tasks SET state='queued',available_at=?,error=?,started_at=NULL WHERE id=?", (utcnow(wait), msg, task['id']))
            else: c.execute("UPDATE tasks SET state=?,finished_at=?,error=? WHERE id=?", ('success' if ok else 'failed', utcnow(), None if ok else msg, task['id']))
        time.sleep(self.delay); return True
    def loop(self):
        while True:
            try: active = self.work_once()
            except Exception: active = False
            if not active: time.sleep(20)

def create_app(config=None):
    config = config or {}; secret = config.get('APP_SECRET', os.getenv('APP_SECRET', 'development-secret-change-me-32chars'))
    key = hmac.new(secret.encode(), b'credential-key', hashlib.sha256).digest()
    app = Flask(__name__); app.config.update(SECRET_KEY=hmac.new(secret.encode(), b'session-key', hashlib.sha256).digest(), DB=config.get('DB', os.getenv('DATABASE_PATH', '/data/app.db')), ADMIN_PASSWORD=config.get('ADMIN_PASSWORD', os.getenv('ADMIN_PASSWORD', 'admin')), TZ=config.get('TZ', os.getenv('TZ', 'Asia/Shanghai')), DELAY=float(config.get('DELAY', os.getenv('REQUEST_INTERVAL_SECONDS', '5'))), COOKIE_SECURE=str(config.get('COOKIE_SECURE', os.getenv('COOKIE_SECURE', 'true'))).lower() == 'true', START_WORKER=config.get('START_WORKER', True))
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=app.config['COOKIE_SECURE'])
    store, runner = Store(app.config['DB'], key), None; store.init(); runner = Runner(store, app.config['TZ'], app.config['DELAY']); app.extensions['store'], app.extensions['runner'] = store, runner
    attempts = {}
    def csrf():
        if 'csrf' not in session: session['csrf'] = secrets.token_urlsafe(24)
        return session['csrf']
    @app.context_processor
    def inject(): return {'csrf': csrf(), 'clock': clock, 'mask': mask}
    @app.before_request
    def verify_csrf():
        if request.method == 'POST' and request.endpoint not in ('login', 'health') and request.form.get('csrf') != session.get('csrf'): abort(400, 'CSRF 校验失败')
    def page(body, **kw):
        rendered = render_template_string(body, **kw)
        rendered = re.sub(r'(<form\b[^>]*\bmethod=post[^>]*>)', r'\1<input type=hidden name=csrf value="' + csrf() + '">', rendered, flags=re.I)
        return render_template_string(LAYOUT, body=rendered)
    def required(f):
        @wraps(f)
        def inner(*a, **kw):
            if not session.get('admin'): return redirect(url_for('login'))
            return f(*a, **kw)
        return inner
    @app.get('/healthz')
    def health(): return {'ok': True}
    @app.route('/login', methods=['GET','POST'])
    def login():
        if request.method == 'POST':
            ip = request.remote_addr or ''; now=time.time(); attempts[ip]=[x for x in attempts.get(ip,[]) if now-x<600]
            if len(attempts[ip]) >= 5: flash('登录尝试过多，请稍后再试')
            elif hmac.compare_digest(request.form.get('password',''), app.config['ADMIN_PASSWORD']): session.clear(); session['admin']=True; csrf(); return redirect(url_for('dashboard'))
            else: attempts[ip].append(now); flash('密码错误')
        return page("<div class=card><h2>管理员登录</h2><form method=post><input type=password name=password autofocus required><button>登录</button></form></div>")
    @app.post('/logout')
    def logout(): session.clear(); return redirect(url_for('login'))
    @app.get('/')
    @required
    def dashboard():
        with store.conn() as c:
            stats=c.execute("SELECT (SELECT count(*) FROM accounts WHERE enabled=1) accounts,(SELECT count(*) FROM plans WHERE enabled=1) plans,(SELECT count(*) FROM tasks WHERE state='queued') queued,(SELECT count(*) FROM tasks WHERE state='failed' AND date(finished_at)=date('now')) failed").fetchone(); recent=c.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 12").fetchall()
        return page("<div class='grid'>{% for k,v in stats.items() %}<div class=card><b>{{k}}</b><h2>{{v}}</h2></div>{% endfor %}</div><div class=card><h2>最近任务</h2>{% include 'none' ignore missing %}<table><tr><th>账户</th><th>来源</th><th>目标</th><th>状态</th><th>错误</th></tr>{% for t in recent %}<tr><td>{{t.account_label}}</td><td>{{t.trigger}}</td><td>{{t.target_steps or '-'}}</td><td>{{t.state}}</td><td class=bad>{{t.error or ''}}</td></tr>{% endfor %}</table></div>", stats=dict(stats), recent=recent)
    @app.route('/accounts', methods=['GET','POST'])
    @required
    def accounts():
        if request.method=='POST':
            try:
                user=request.form['username'].strip(); pwd=request.form['password']; plan=request.form.get('plan_id') or None
                if not user or not pwd: raise ValueError('账号和密码必填')
                with store.conn() as c: c.execute("INSERT INTO accounts(username,note,secret,plan_id,created_at,updated_at) VALUES(?,?,?,?,?,?)", (user,request.form.get('note','').strip(),store.seal({'username':user,'password':pwd}),plan,utcnow(),utcnow()))
                flash('账户已添加')
            except Exception as e: flash('添加失败：'+str(e))
            return redirect(url_for('accounts'))
        with store.conn() as c: rows=c.execute("SELECT a.*,p.name plan_name FROM accounts a LEFT JOIN plans p ON p.id=a.plan_id ORDER BY a.id DESC").fetchall(); plans=c.execute('SELECT * FROM plans ORDER BY name').fetchall()
        body = '''<div class=card><h2>添加账户</h2><form method=post class=grid><input type=hidden name=csrf value='{{csrf}}'><input name=username placeholder='Zepp Life 手机号或邮箱' required><input name=password type=password placeholder=密码 required><input name=note placeholder=备注><select name=plan_id><option value=''>不分配计划</option>{% for p in plans %}<option value={{p.id}}>{{p.name}}</option>{% endfor %}</select><button>添加</button></form></div><div class=card><h2>批量导入</h2><form method=post action='{{url_for("import_accounts")}}'><select name=plan_id><option value=''>不分配计划</option>{% for p in plans %}<option value={{p.id}}>{{p.name}}</option>{% endfor %}</select><textarea name=rows placeholder='每行：账号,密码,备注'></textarea><button>导入</button></form></div><div class=card><h2>账户</h2><form method=post action='{{url_for("manual_run")}}'><table><tr><th></th><th>账号</th><th>备注/计划</th><th>状态</th><th>操作</th></tr>{% for a in rows %}<tr><td><input type=checkbox name=account_id value={{a.id}} {% if not a.enabled %}disabled{% endif %}></td><td>{{mask(a.username)}}</td><td>{{a.note}}<br><span class=muted>{{a.plan_name or '未分配'}}</span></td><td>{{'启用' if a.enabled else '停用'}}</td><td><button formaction='{{url_for("test_account",aid=a.id)}}'>测试</button><button formaction='{{url_for("toggle_account",aid=a.id)}}'>{{'停用' if a.enabled else '启用'}}</button><button formaction='{{url_for("delete_account",aid=a.id)}}' onclick='return confirm("删除账户及其凭据？")'>删除</button></td></tr>{% endfor %}</table><p>手动目标：<input name=low type=number min=1 required placeholder=最小步数> - <input name=high type=number min=1 placeholder='最大步数（可选）'><button>执行选中账户</button></p></form></div>'''
        body = body.replace("<button formaction='{{url_for(\"test_account\",aid=a.id)}}'>", "<a href='{{url_for(\"edit_account\",aid=a.id)}}'>编辑</a> <button formaction='{{url_for(\"test_account\",aid=a.id)}}'>")
        return page(body, rows=rows, plans=plans)
    @app.route('/accounts/<int:aid>/edit', methods=['GET', 'POST'])
    @required
    def edit_account(aid):
        with store.conn() as c:
            account = c.execute('SELECT * FROM accounts WHERE id=?', (aid,)).fetchone()
            plans = c.execute('SELECT * FROM plans ORDER BY name').fetchall()
        if not account: abort(404)
        if request.method == 'POST':
            try:
                secret = store.open(account['secret']); password = request.form.get('password')
                if password: secret['password'] = password
                with store.conn() as c: c.execute('UPDATE accounts SET note=?,plan_id=?,secret=?,updated_at=? WHERE id=?', (request.form.get('note','').strip(), request.form.get('plan_id') or None, store.seal(secret), utcnow(), aid))
                flash('账户已更新'); return redirect(url_for('accounts'))
            except Exception as e: flash('更新失败：' + str(e))
        return page('''<div class=card><h2>编辑 {{mask(account.username)}}</h2><form method=post><label>备注 <input name=note value='{{account.note}}'></label><label>新密码（留空则保持不变） <input type=password name=password></label><select name=plan_id><option value=''>不分配计划</option>{% for p in plans %}<option value={{p.id}} {% if account.plan_id==p.id %}selected{% endif %}>{{p.name}}</option>{% endfor %}</select><button>保存</button></form></div>''', account=account, plans=plans)
    @app.post('/accounts/import')
    @required
    def import_accounts():
        count=0
        try:
            rows=csv.reader(io.StringIO(request.form.get('rows',''))); plan=request.form.get('plan_id') or None
            with store.conn() as c:
                for row in rows:
                    if len(row)<2 or not row[0].strip() or not row[1]: continue
                    user=row[0].strip(); c.execute("INSERT OR IGNORE INTO accounts(username,note,secret,plan_id,created_at,updated_at) VALUES(?,?,?,?,?,?)", (user,(row[2] if len(row)>2 else '').strip(),store.seal({'username':user,'password':row[1]}),plan,utcnow(),utcnow())); count += c.execute('SELECT changes()').fetchone()[0]
            flash(f'已导入 {count} 个账户，重复账号已跳过')
        except Exception as e: flash('导入失败：'+str(e))
        return redirect(url_for('accounts'))
    @app.post('/accounts/<int:aid>/toggle')
    @required
    def toggle_account(aid):
        with store.conn() as c: c.execute('UPDATE accounts SET enabled=1-enabled,updated_at=? WHERE id=?',(utcnow(),aid))
        return redirect(url_for('accounts'))
    @app.post('/accounts/<int:aid>/delete')
    @required
    def delete_account(aid):
        with store.conn() as c: c.execute('DELETE FROM accounts WHERE id=?',(aid,))
        flash('账户及已保存凭据已删除'); return redirect(url_for('accounts'))
    @app.post('/accounts/<int:aid>/test')
    @required
    def test_account(aid):
        with store.conn() as c:
            a=c.execute('SELECT * FROM accounts WHERE id=?',(aid,)).fetchone()
            if a: c.execute("INSERT INTO tasks(account_id,account_label,trigger,available_at,created_at) VALUES(?,?,?,?,?)",(aid,mask(a['username']),'测试',utcnow(),utcnow())); flash('测试已入队')
        return redirect(url_for('accounts'))
    @app.post('/run')
    @required
    def manual_run():
        try: flash(f'已入队 {runner.enqueue(request.form.getlist("account_id"),request.form["low"],request.form.get("high"))} 个任务')
        except Exception as e: flash('无法入队：'+str(e))
        return redirect(url_for('accounts'))
    @app.route('/plans',methods=['GET','POST'])
    @required
    def plans():
        if request.method=='POST':
            try:
                with store.conn() as c: c.execute('INSERT INTO plans(name,created_at) VALUES(?,?)',(request.form['name'].strip(),utcnow()))
                flash('计划已创建')
            except Exception as e: flash('创建失败：'+str(e))
            return redirect(url_for('plans'))
        with store.conn() as c: rows=c.execute("SELECT p.*,count(a.id) accounts,count(pp.id) points FROM plans p LEFT JOIN accounts a ON a.plan_id=p.id LEFT JOIN plan_points pp ON pp.plan_id=p.id GROUP BY p.id ORDER BY p.name").fetchall()
        body = '''<div class=card><h2>新建计划</h2><form method=post><input type=hidden name=csrf value='{{csrf}}'><input name=name required placeholder='计划名称'><button>创建</button></form></div><div class=card><table><tr><th>名称</th><th>账户</th><th>时间点</th><th>状态</th><th></th></tr>{% for p in rows %}<tr><td>{{p.name}}</td><td>{{p.accounts}}</td><td>{{p.points}}</td><td>{{'启用' if p.enabled else '停用'}}</td><td><a href='{{url_for("plan_detail",pid=p.id)}}'>编辑</a></td></tr>{% endfor %}</table></div>'''
        return page(body, rows=rows)
    @app.route('/plans/<int:pid>',methods=['GET','POST'])
    @required
    def plan_detail(pid):
        with store.conn() as c: plan=c.execute('SELECT * FROM plans WHERE id=?',(pid,)).fetchone()
        if not plan: abort(404)
        if request.method=='POST':
            try:
                at=minute(request.form['at']); low,high=parse_target(request.form['low'],request.form.get('high'))
                with store.conn() as c:
                    points=c.execute('SELECT * FROM plan_points WHERE plan_id=? ORDER BY at_minute',(pid,)).fetchall()
                    candidate=[(p['at_minute'],p['low_steps'],p['high_steps']) for p in points]+[(at,low,high)]; candidate.sort()
                    if any(candidate[i][1] < candidate[i-1][2] for i in range(1,len(candidate))): raise ValueError('时间点目标必须单调递增')
                    c.execute('INSERT INTO plan_points(plan_id,at_minute,low_steps,high_steps) VALUES(?,?,?,?)',(pid,at,low,high))
                flash('时间点已添加')
            except Exception as e: flash('添加失败：'+str(e))
            return redirect(url_for('plan_detail',pid=pid))
        with store.conn() as c: points=c.execute('SELECT * FROM plan_points WHERE plan_id=? ORDER BY at_minute',(pid,)).fetchall()
        body = '''<div class=card><h2>{{plan.name}}</h2><form method=post><input type=hidden name=csrf value='{{csrf}}'><input name=at type=time required><input name=low type=number min=1 placeholder='固定值或最小值' required><input name=high type=number min=1 placeholder='最大值（可选）'><button>添加时间点</button></form><p class=muted>同一时间点仅执行一次；随机区间在入队时抽取。</p><table><tr><th>时间</th><th>目标</th><th></th></tr>{% for p in points %}<tr><td>{{clock(p.at_minute)}}</td><td>{{p.low_steps}}{{'' if p.low_steps==p.high_steps else ' – '+p.high_steps|string}}</td><td><form class=inline method=post action='{{url_for("delete_point",pid=pid,point_id=p.id)}}'><button>删除</button></form></td></tr>{% endfor %}</table><form method=post action='{{url_for("delete_plan",pid=pid)}}'><button onclick='return confirm("删除计划？已分配账户将变为未分配。")'>删除计划</button></form></div>'''
        return page(body, plan=plan, points=points)
    @app.post('/plans/<int:pid>/points/<int:point_id>/delete')
    @required
    def delete_point(pid,point_id):
        with store.conn() as c: c.execute('DELETE FROM plan_points WHERE id=? AND plan_id=?',(point_id,pid))
        return redirect(url_for('plan_detail',pid=pid))
    @app.post('/plans/<int:pid>/delete')
    @required
    def delete_plan(pid):
        with store.conn() as c: c.execute('DELETE FROM plans WHERE id=?',(pid,))
        flash('计划已删除'); return redirect(url_for('plans'))
    @app.get('/history')
    @required
    def history():
        with store.conn() as c: rows=c.execute('SELECT * FROM tasks ORDER BY id DESC LIMIT 200').fetchall()
        body = '''<div class=card><h2>执行记录</h2><table><tr><th>时间</th><th>账户</th><th>来源</th><th>目标</th><th>尝试</th><th>状态</th><th>信息</th><th></th></tr>{% for t in rows %}<tr><td>{{t.created_at}}</td><td>{{t.account_label}}</td><td>{{t.trigger}}</td><td>{{t.target_steps or '-'}}</td><td>{{t.attempts}}</td><td class="{{'ok' if t.state=='success' else 'bad' if t.state=='failed' else ''}}">{{t.state}}</td><td>{{t.error or ''}}</td><td>{% if t.state=='failed' %}<form method=post action='{{url_for("retry_task",tid=t.id)}}'><button>重试</button></form>{% endif %}</td></tr>{% endfor %}</table></div>'''
        return page(body, rows=rows)
    @app.post('/tasks/<int:tid>/retry')
    @required
    def retry_task(tid):
        with store.conn() as c: c.execute("UPDATE tasks SET state='queued',attempts=0,available_at=?,started_at=NULL,finished_at=NULL,error=NULL WHERE id=? AND state='failed'",(utcnow(),tid))
        flash('任务已重新入队'); return redirect(url_for('history'))
    if app.config['START_WORKER']: threading.Thread(target=runner.loop,daemon=True,name='step-worker').start()
    return app

if __name__ == '__main__':
    from waitress import serve
    if len(os.getenv('APP_SECRET','')) < 32 or not os.getenv('ADMIN_PASSWORD'): raise SystemExit('APP_SECRET（至少32字符）和 ADMIN_PASSWORD 为必填项')
    serve(create_app(), host='0.0.0.0', port=int(os.getenv('PORT','8000')))
