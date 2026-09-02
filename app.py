"""Private Zepp Life step scheduler.  Protocol helper derives from mimotion."""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import ipaddress
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
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from Crypto.Cipher import AES
from flask import Flask, abort, flash, redirect, render_template_string, request, session, url_for
import requests
from vendor.util import zepp_helper

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS plans (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan_points (id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE, at_minute INTEGER NOT NULL, low_steps INTEGER NOT NULL, high_steps INTEGER NOT NULL, UNIQUE(plan_id, at_minute));
CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, note TEXT NOT NULL DEFAULT '', secret TEXT NOT NULL, token_secret TEXT, plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL, account_label TEXT NOT NULL, point_id INTEGER REFERENCES plan_points(id) ON DELETE SET NULL, run_date TEXT, trigger TEXT NOT NULL, target_steps INTEGER, state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT, created_at TEXT NOT NULL, UNIQUE(account_id, point_id, run_date));
CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), proxy_secret TEXT, random_enabled INTEGER NOT NULL DEFAULT 0, random_min INTEGER NOT NULL DEFAULT -100, random_max INTEGER NOT NULL DEFAULT 100);
CREATE INDEX IF NOT EXISTS task_queue ON tasks(state, available_at, id);
CREATE INDEX IF NOT EXISTS task_created_at ON tasks(created_at);
"""

LAYOUT = """<!doctype html><html lang=zh-CN><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>Zepp Life 步数计划</title><style>
:root{--ink:#172033;--muted:#667085;--line:#e5e9f2;--brand:#4f46e5;--brand-dark:#3730a3;--surface:#fff;--canvas:#f5f7fb;--danger:#b42318;--success:#087443}*{box-sizing:border-box}body{font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;color:var(--ink);background:var(--canvas)}.shell{max-width:1180px;margin:auto;padding:20px}.topbar{display:flex;align-items:center;gap:20px;margin-bottom:20px}.brand{font-size:18px;font-weight:750;letter-spacing:-.02em}.nav{display:flex;gap:4px;align-items:center}.nav a{padding:8px 10px;border-radius:8px;color:var(--muted);text-decoration:none;font-weight:600}.nav a:hover{background:#eef2ff;color:var(--brand)}.topbar form{margin-left:auto}.card{background:var(--surface);padding:20px;margin:14px 0;border:1px solid var(--line);border-radius:14px;box-shadow:0 3px 12px #17203308}.card h2{margin:0 0 14px;font-size:17px}.card p:last-child{margin-bottom:0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}.stat b{color:var(--muted);font-size:13px}.stat h2{margin:4px 0 0;font-size:28px}table{width:100%;border-collapse:collapse}td,th{padding:11px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{font-size:12px;color:var(--muted);font-weight:700}tr:last-child td{border-bottom:0}.table-wrap{overflow-x:auto}input,select,textarea,button{font:inherit;border:1px solid #d0d5dd;border-radius:8px;padding:9px 10px;margin:3px;background:#fff;color:var(--ink)}input:focus,select:focus,textarea:focus,button:focus,a:focus{outline:3px solid #c7d2fe;outline-offset:2px}textarea{width:100%;min-height:100px}button{cursor:pointer;background:var(--brand);border-color:var(--brand);color:#fff;font-weight:650}button:hover{background:var(--brand-dark)}button.secondary{background:#fff;color:var(--brand);border-color:#c7d2fe}button.danger{background:#fff;color:var(--danger);border-color:#fecaca}.ok{color:var(--success)}.bad{color:var(--danger)}.muted{color:var(--muted)}.flash{padding:11px 13px;background:#fffaeb;border:1px solid #fedf89;border-radius:10px;margin:10px 0}.inline{display:inline}.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 12px}.toolbar .spacer{flex:1}.badge{display:inline-block;padding:2px 8px;border-radius:999px;background:#eef2ff;color:var(--brand);font-size:12px;font-weight:700}.badge.success{background:#ecfdf3;color:var(--success)}.badge.failed{background:#fef3f2;color:var(--danger)}.empty{padding:24px;text-align:center;color:var(--muted)}.link{color:var(--brand);font-weight:650;text-decoration:none}.actions{white-space:nowrap}@media(max-width:700px){body{padding-bottom:76px}.shell{padding:14px}.topbar{margin-bottom:12px}.topbar form{margin-left:auto}.nav{position:fixed;z-index:2;left:0;right:0;bottom:0;padding:8px;background:#fffffff2;border-top:1px solid var(--line);justify-content:space-around;box-shadow:0 -4px 16px #1720330d}.nav a{font-size:12px;padding:7px 5px}.card{padding:15px;border-radius:12px}.grid{grid-template-columns:1fr}.table-wrap{overflow:visible}table.responsive,table.responsive tbody,table.responsive tr,table.responsive td{display:block;width:100%}table.responsive thead{display:none}table.responsive tr{padding:8px 0;border-bottom:1px solid var(--line)}table.responsive tr:last-child{border-bottom:0}table.responsive td{display:flex;gap:12px;justify-content:space-between;border:0;padding:6px 2px;text-align:right}table.responsive td:before{content:attr(data-label);text-align:left;color:var(--muted);font-size:12px;font-weight:700}.actions{white-space:normal}.toolbar>*{flex:1 1 135px}.toolbar .spacer{display:none}}
</style></head><body><div class=shell><header class=topbar><strong class=brand>Zepp Life 步数计划</strong>{% if session.get('admin') %}<nav class=nav><a href='{{url_for("dashboard")}}'>仪表盘</a><a href='{{url_for("accounts")}}'>账户</a><a href='{{url_for("plans")}}'>计划</a><a href='{{url_for("history")}}'>执行记录</a><a href='{{url_for("settings_page")}}'>设置</a></nav><form method=post action='{{url_for("logout")}}'><input type=hidden name=csrf value='{{csrf}}'><button class=secondary>退出</button></form>{% endif %}</header>
{% with m=get_flashed_messages() %}{% for x in m %}<div class=flash>{{x}}</div>{% endfor %}{% endwith %}{{body|safe}}</div></body></html>"""

def utcnow(delay=0): return (datetime.now(UTC) + timedelta(seconds=delay)).replace(tzinfo=None, microsecond=0).isoformat()
def localtime(value, tz):
    if not value: return ''
    return datetime.fromisoformat(value).replace(tzinfo=UTC).astimezone(ZoneInfo(tz)).strftime('%Y-%m-%d %H:%M:%S')
def mask(value):
    value = str(value); n = max(1, len(value) // 3)
    return value[:n] + "***" + value[-n:]
def minute(value):
    h, m = map(int, value.split(":"));
    if not 0 <= h < 24 or not 0 <= m < 60: raise ValueError("时间必须是 HH:MM")
    return h * 60 + m
def clock(value): return f"{value // 60:02d}:{value % 60:02d}"
def parse_steps(value):
    value = int(value)
    if value < 1: raise ValueError("固定步数必须为正整数")
    return value
def parse_random_range(low, high):
    low, high = int(low), int(high)
    if low > high: raise ValueError("随机下限不能大于上限")
    return low, high
def parse_proxy_url(value):
    value = value.strip(); parts = urlsplit(value)
    if parts.scheme not in ('socks5', 'socks5h') or not parts.hostname or parts.path not in ('', '/') or parts.query or parts.fragment: raise ValueError("代理地址应为 socks5h://[用户名:密码@]主机:端口")
    try: port = parts.port
    except ValueError: port = None
    if port is None or not 1 <= port <= 65535: raise ValueError("代理端口必须为 1–65535")
    return value
def mask_proxy(value):
    if not value: return '未设置'
    parts = urlsplit(value); host = parts.hostname or ''
    if ':' in host: host = '[' + host + ']'
    return f"{parts.scheme}://{'***@' if parts.username else ''}{host}:{parts.port}"
def safe_error(exc, proxy=''):
    message = str(exc)
    if proxy:
        message = message.replace(proxy, '[代理]')
        parts = urlsplit(proxy)
        for value in (parts.username, parts.password, parts.netloc):
            if value: message = message.replace(value, '***')
    return message[:300]

class Store:
    def __init__(self, path, key): self.path, self.key = str(path), key
    def conn(self):
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None); con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA foreign_keys=ON"); return con
    def init(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)
            c.execute("INSERT OR IGNORE INTO settings(id) VALUES(1)")
            c.execute("UPDATE plan_points SET high_steps=low_steps WHERE high_steps<>low_steps")
            c.execute("UPDATE tasks SET state='queued', started_at=NULL WHERE state='running'")
            self.prune_tasks(c)
    def prune_tasks(self, con=None):
        if con: return con.execute("DELETE FROM tasks WHERE created_at<?", (utcnow(-7 * 24 * 60 * 60),)).rowcount
        with self.conn() as c: return self.prune_tasks(c)
    def seal(self, value):
        cipher = AES.new(self.key, AES.MODE_GCM); data, tag = cipher.encrypt_and_digest(json.dumps(value).encode())
        return base64.urlsafe_b64encode(cipher.nonce + tag + data).decode()
    def open(self, value):
        raw = base64.urlsafe_b64decode(value); cipher = AES.new(self.key, AES.MODE_GCM, nonce=raw[:16])
        return json.loads(cipher.decrypt_and_verify(raw[32:], raw[16:32]).decode())
    def settings(self):
        with self.conn() as c: return dict(c.execute("SELECT * FROM settings WHERE id=1").fetchone())
    def proxy_url(self):
        value = self.settings()['proxy_secret']
        return self.open(value)['url'] if value else None
    def save_proxy(self, value):
        with self.conn() as c: c.execute("UPDATE settings SET proxy_secret=? WHERE id=1", (self.seal({'url': value}),))
    def clear_proxy(self):
        with self.conn() as c: c.execute("UPDATE settings SET proxy_secret=NULL WHERE id=1")
    def save_random(self, enabled, low, high):
        with self.conn() as c: c.execute("UPDATE settings SET random_enabled=?,random_min=?,random_max=? WHERE id=1", (int(enabled), low, high))

class Runner:
    def __init__(self, store, tz, delay): self.store, self.tz, self.delay = store, ZoneInfo(tz), delay
    def client(self, proxy=None):
        proxy = self.store.proxy_url() if proxy is None else proxy
        client = requests.Session(); client.trust_env = False
        if proxy: client.proxies.update({'http': proxy, 'https': proxy})
        return client
    def target(self, base, settings):
        if settings['random_enabled']: return base + random.randint(settings['random_min'], settings['random_max'])
        return base
    def valid_base(self, base, settings):
        if base < 1: raise ValueError("固定步数必须为正整数")
        if settings['random_enabled'] and base + settings['random_min'] < 1: raise ValueError("固定步数加随机下限必须至少为 1")
    def add_task(self, c, account, base, settings, trigger, point_id=None, run_date=None):
        self.valid_base(base, settings); target = self.target(base, settings)
        previous = c.execute("SELECT MAX(target_steps) FROM tasks WHERE account_id=? AND run_date=? AND state='success'", (account['id'], run_date)).fetchone()[0] if run_date else None
        skipped = previous is not None and target < previous
        values = (account['id'], mask(account['username']), point_id, run_date, trigger, target, 'skipped' if skipped else 'queued', utcnow(), utcnow() if skipped else None, '目标低于当天已成功步数，已跳过' if skipped else None, utcnow())
        sql = "INSERT {} INTO tasks(account_id,account_label,point_id,run_date,trigger,target_steps,state,available_at,finished_at,error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)".format("OR IGNORE" if point_id else "")
        changed = c.execute(sql, values).rowcount
        return ('skipped' if skipped else 'queued') if changed else None
    def enqueue_scheduled(self):
        now = datetime.now(self.tz); date, now_min = now.date().isoformat(), now.hour * 60 + now.minute
        settings = self.store.settings()
        with self.store.conn() as c:
            rows = c.execute("""SELECT a.*, p.id point_id,p.at_minute,p.low_steps,p.high_steps FROM accounts a JOIN plans pl ON pl.id=a.plan_id AND pl.enabled=1 JOIN plan_points p ON p.plan_id=pl.id WHERE a.enabled=1 AND p.at_minute<=? ORDER BY a.id,p.at_minute""", (now_min,)).fetchall()
            latest = {}
            for r in rows: latest[r['id']] = r
            for r in latest.values():
                self.add_task(c, r, r['low_steps'], settings, '计划', r['point_id'], date)
    def enqueue(self, ids, steps, trigger='手动'):
        steps = parse_steps(steps); now = datetime.now(self.tz); date = now.date().isoformat(); queued = skipped = 0; settings = self.store.settings()
        with self.store.conn() as c:
            for aid in ids:
                a = c.execute("SELECT * FROM accounts WHERE id=? AND enabled=1", (aid,)).fetchone()
                if not a: continue
                state = self.add_task(c, a, steps, settings, trigger, run_date=date)
                queued += state == 'queued'; skipped += state == 'skipped'
        return queued, skipped
    def claim(self):
        with self.store.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM tasks WHERE state='queued' AND available_at<=? ORDER BY id LIMIT 1", (utcnow(),)).fetchone()
            if row: c.execute("UPDATE tasks SET state='running',attempts=attempts+1,started_at=? WHERE id=?", (utcnow(), row['id']))
            c.commit(); return row
    def credentials(self, account): return self.store.open(account['secret']), self.store.open(account['token_secret']) if account['token_secret'] else {}
    def authenticate(self, account, client):
        secret, tokens = self.credentials(account); user, password = secret['username'], secret['password']
        user = user if user.startswith('+86') or '@' in user else '+86' + user; phone = user.startswith('+86')
        device = tokens.get('device_id') or str(uuid.uuid4()); app_token = tokens.get('app_token'); uid = tokens.get('user_id')
        if app_token:
            try: ok, _ = zepp_helper.check_app_token(app_token, client=client)
            except Exception: ok = False
        else: ok = False
        if not ok and tokens.get('login_token'):
            app_token, _ = zepp_helper.grant_app_token(tokens['login_token'], client=client); ok = bool(app_token)
        if not ok and tokens.get('access_token'):
            login, app_token, uid, _ = zepp_helper.grant_login_tokens(tokens['access_token'], device, phone, client=client)
            if login: tokens['login_token'] = login; ok = True
        if not ok:
            access, err = zepp_helper.login_access_token(user, password, client=client)
            if not access: raise ValueError("认证失败：" + (err or "账号或密码不可用"))
            login, app_token, uid, err = zepp_helper.grant_login_tokens(access, device, phone, client=client)
            if not login: raise ValueError("认证失败：" + (err or "无法获取登录令牌"))
            tokens.update(access_token=access, login_token=login); ok = True
        tokens.update(app_token=app_token, user_id=uid, device_id=device)
        if not uid or not app_token: raise ValueError("认证失败：缺少有效身份信息")
        if not tokens.get('bound_device_id'):
            tokens['bound_device_id'] = zepp_helper.get_user_device_id(app_token, uid, client=client)
        with self.store.conn() as c: c.execute("UPDATE accounts SET token_secret=?,updated_at=? WHERE id=?", (self.store.seal(tokens), utcnow(), account['id']))
        return app_token, uid, tokens.get('bound_device_id')
    def execute(self, task):
        with self.store.conn() as c: account = c.execute("SELECT * FROM accounts WHERE id=?", (task['account_id'],)).fetchone()
        if not account: return False, "账户已删除", False
        try:
            client = self.client(); app_token, uid, device = self.authenticate(account, client)
            if task['trigger'] == '测试': return True, "登录和设备检查成功", False
            ok, message = zepp_helper.post_fake_brand_data(str(task['target_steps']), app_token, uid, device, client=client)
            return ok, ("提交成功" if ok else "提交失败：" + str(message)), not ok and any(x in str(message) for x in ('429', '500', '502', '503', '504'))
        except Exception as exc:
            message = safe_error(exc, self.store.proxy_url() or ''); return False, message, not message.startswith('认证失败')
    def work_once(self):
        self.store.prune_tasks()
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
    def inject(): return {'csrf': csrf(), 'clock': clock, 'mask': mask, 'localtime': lambda value: localtime(value, app.config['TZ']), 'timezone': app.config['TZ'], 'state_labels': {'queued': '排队中', 'running': '执行中', 'success': '成功', 'failed': '失败', 'skipped': '已跳过'}}
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
        local_midnight = datetime.now(runner.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        day_start = local_midnight.astimezone(UTC).replace(tzinfo=None).isoformat()
        day_end = (local_midnight + timedelta(days=1)).astimezone(UTC).replace(tzinfo=None).isoformat()
        with store.conn() as c:
            stats=c.execute("SELECT (SELECT count(*) FROM accounts WHERE enabled=1) accounts,(SELECT count(*) FROM plans WHERE enabled=1) plans,(SELECT count(*) FROM tasks WHERE state='queued') queued,(SELECT count(*) FROM tasks WHERE state='failed' AND finished_at>=? AND finished_at<?) failed", (day_start, day_end)).fetchone(); recent=c.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 12").fetchall()
        return page("<div class='grid'>{% for k,v in stats.items() %}<div class='card stat'><b>{{k}}</b><h2>{{v}}</h2></div>{% endfor %}</div><div class=card><h2>最近任务</h2><div class=table-wrap><table class=responsive><thead><tr><th>账户</th><th>来源</th><th>目标</th><th>状态</th><th>错误</th></tr></thead><tbody>{% for t in recent %}<tr><td data-label=账户>{{t.account_label}}</td><td data-label=来源>{{t.trigger}}</td><td data-label=目标>{{t.target_steps or '-'}}</td><td data-label=状态><span class='badge {{t.state}}'>{{state_labels.get(t.state,t.state)}}</span></td><td data-label=错误 class=bad>{{t.error or ''}}</td></tr>{% else %}<tr><td colspan=5 class=empty>暂无任务。</td></tr>{% endfor %}</tbody></table></div></div>", stats=dict(stats), recent=recent)
    @app.get('/settings')
    @required
    def settings_page():
        values = store.settings()
        body = '''<div class=card><h2>SOCKS5 代理</h2><p class=muted>当前：{{proxy}}</p><form method=post action='{{url_for("save_proxy")}}' class=toolbar><input name=proxy_url required placeholder='socks5h://[用户名:密码@]主机:端口'><button>保存代理</button></form><form class=inline method=post action='{{url_for("test_proxy")}}'><button class=secondary>验证已保存代理</button></form> <form class=inline method=post action='{{url_for("clear_proxy")}}'><button class=danger onclick='return confirm("清除代理设置？")'>清除代理</button></form><p class=muted>推荐 socks5h：域名解析也经代理进行。</p></div><div class=card><h2>随机步数偏移</h2><form method=post action='{{url_for("save_random")}}' class=toolbar><label><input type=checkbox name=enabled value=1 {% if values.random_enabled %}checked{% endif %}> 启用随机</label><input type=number name=low value='{{values.random_min}}' required placeholder='下限'><input type=number name=high value='{{values.random_max}}' required placeholder='上限'><button>保存随机设置</button></form><p class=muted>启用后，每个新任务的目标为固定步数加上此范围内重新抽取的随机数。</p></div>'''
        return page(body, values=values, proxy=mask_proxy(store.proxy_url()))
    @app.post('/settings/proxy')
    @required
    def save_proxy():
        try: store.save_proxy(parse_proxy_url(request.form.get('proxy_url', ''))); flash('代理已保存，请点击验证确认出口 IP')
        except Exception as e: flash('代理保存失败：' + safe_error(e))
        return redirect(url_for('settings_page'))
    @app.post('/settings/proxy/test')
    @required
    def test_proxy():
        try:
            proxy = store.proxy_url()
            if not proxy: raise ValueError('请先保存代理地址')
            response = runner.client(proxy).get('https://api.ipify.org?format=json', timeout=10); response.raise_for_status()
            ip = response.json().get('ip'); ipaddress.ip_address(ip)
            flash('代理验证成功，出口 IP：' + ip)
        except Exception as e: flash('代理验证失败：' + safe_error(e, store.proxy_url() or ''))
        return redirect(url_for('settings_page'))
    @app.post('/settings/proxy/clear')
    @required
    def clear_proxy():
        store.clear_proxy(); flash('代理已清除')
        return redirect(url_for('settings_page'))
    @app.post('/settings/random')
    @required
    def save_random():
        try:
            low, high = parse_random_range(request.form['low'], request.form['high']); enabled = bool(request.form.get('enabled'))
            if enabled:
                with store.conn() as c:
                    if c.execute('SELECT 1 FROM plan_points WHERE low_steps+?<1 LIMIT 1', (low,)).fetchone(): raise ValueError('已有计划的固定步数加随机下限小于 1')
            store.save_random(enabled, low, high); flash('随机步数设置已保存')
        except Exception as e: flash('随机设置保存失败：' + str(e))
        return redirect(url_for('settings_page'))
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
        body = '''<div class=card><h2>添加账户</h2><form method=post class=grid><input type=hidden name=csrf value='{{csrf}}'><input name=username placeholder='Zepp Life 手机号或邮箱' required><input name=password type=password placeholder=密码 required><input name=note placeholder=备注><select name=plan_id><option value=''>不分配计划</option>{% for p in plans %}<option value={{p.id}}>{{p.name}}</option>{% endfor %}</select><button>添加</button></form></div><div class=card><h2>批量导入</h2><form method=post action='{{url_for("import_accounts")}}'><select name=plan_id><option value=''>不分配计划</option>{% for p in plans %}<option value={{p.id}}>{{p.name}}</option>{% endfor %}</select><textarea name=rows placeholder='每行：账号,密码,备注'></textarea><button>导入</button></form></div><div class=card><h2>账户</h2><form method=post action='{{url_for("manual_run")}}'><div class=toolbar><label><input id=select-all type=checkbox> 全选</label><select name=plan_id><option value=''>取消计划分配</option>{% for p in plans %}<option value={{p.id}}>{{p.name}}</option>{% endfor %}</select><button class=secondary formnovalidate formaction='{{url_for("bulk_assign_plan")}}'>批量配置计划</button></div><div class=table-wrap><table class=responsive><thead><tr><th></th><th>账号</th><th>备注/计划</th><th>状态</th><th>操作</th></tr></thead><tbody>{% for a in rows %}<tr><td data-label=选择><input class=account-select type=checkbox name=account_id value={{a.id}}></td><td data-label=账号>{{mask(a.username)}}</td><td data-label=备注/计划>{{a.note}}<br><span class=muted>{{a.plan_name or '未分配'}}</span></td><td data-label=状态><span class=badge>{{'启用' if a.enabled else '停用'}}</span></td><td data-label=操作 class=actions><a class=link href='{{url_for("edit_account",aid=a.id)}}'>编辑</a> <button formnovalidate formaction='{{url_for("test_account",aid=a.id)}}'>测试</button><button class=secondary formnovalidate formaction='{{url_for("toggle_account",aid=a.id)}}'>{{'停用' if a.enabled else '启用'}}</button><button class=danger formnovalidate formaction='{{url_for("delete_account",aid=a.id)}}' onclick='return confirm("删除账户及其凭据？")'>删除</button></td></tr>{% else %}<tr><td colspan=5 class=empty>还没有账户，先添加或批量导入。</td></tr>{% endfor %}</tbody></table></div><div class=toolbar><span class=muted>手动执行仅处理启用账户。</span><span class=spacer></span><input name=steps type=number min=1 required placeholder=固定步数><button>执行选中账户</button></div></form></div><script>document.getElementById('select-all')?.addEventListener('change',function(){document.querySelectorAll('.account-select').forEach(function(box){box.checked=this.checked},this)})</script>'''
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
    @app.post('/accounts/plan')
    @required
    def bulk_assign_plan():
        ids = request.form.getlist('account_id'); plan_id = request.form.get('plan_id') or None
        try:
            if not ids: raise ValueError('请至少选择一个账户')
            with store.conn() as c:
                if plan_id and not c.execute('SELECT 1 FROM plans WHERE id=?', (plan_id,)).fetchone(): raise ValueError('计划不存在')
                marks = ','.join('?' for _ in ids)
                count = c.execute(f'UPDATE accounts SET plan_id=?,updated_at=? WHERE id IN ({marks})', (plan_id, utcnow(), *ids)).rowcount
            flash(f'已更新 {count} 个账户的计划')
        except Exception as e: flash('批量配置失败：' + str(e))
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
        try:
            queued, skipped = runner.enqueue(request.form.getlist("account_id"), request.form["steps"])
            flash(f'已入队 {queued} 个任务，已跳过 {skipped} 个任务')
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
        body = '''<div class=card><h2>新建计划</h2><form method=post><input type=hidden name=csrf value='{{csrf}}'><input name=name required placeholder='计划名称'><button>创建</button></form></div><div class=card><div class=table-wrap><table class=responsive><thead><tr><th>名称</th><th>账户</th><th>时间点</th><th>状态</th><th></th></tr></thead><tbody>{% for p in rows %}<tr><td data-label=名称>{{p.name}}</td><td data-label=账户>{{p.accounts}}</td><td data-label=时间点>{{p.points}}</td><td data-label=状态><span class=badge>{{'启用' if p.enabled else '停用'}}</span></td><td data-label=操作><a class=link href='{{url_for("plan_detail",pid=p.id)}}'>编辑</a> <form class=inline method=post action='{{url_for("delete_plan",pid=p.id)}}'><button class=danger onclick='return confirm("删除计划？已分配账户将变为未分配。")'>删除</button></form></td></tr>{% else %}<tr><td colspan=5 class=empty>还没有计划。</td></tr>{% endfor %}</tbody></table></div></div>'''
        return page(body, rows=rows)
    @app.route('/plans/<int:pid>',methods=['GET','POST'])
    @required
    def plan_detail(pid):
        with store.conn() as c: plan=c.execute('SELECT * FROM plans WHERE id=?',(pid,)).fetchone()
        if not plan: abort(404)
        if request.method=='POST':
            try:
                at=minute(request.form['at']); steps=parse_steps(request.form['steps']); settings=store.settings()
                runner.valid_base(steps, settings)
                with store.conn() as c:
                    points=c.execute('SELECT * FROM plan_points WHERE plan_id=? ORDER BY at_minute',(pid,)).fetchall()
                    candidate=[(p['at_minute'],p['low_steps']) for p in points]+[(at,steps)]; candidate.sort()
                    if any(candidate[i][1] < candidate[i-1][1] for i in range(1,len(candidate))): raise ValueError('时间点固定步数必须单调递增')
                    c.execute('INSERT INTO plan_points(plan_id,at_minute,low_steps,high_steps) VALUES(?,?,?,?)',(pid,at,steps,steps))
                flash('时间点已添加')
            except Exception as e: flash('添加失败：'+str(e))
            return redirect(url_for('plan_detail',pid=pid))
        with store.conn() as c: points=c.execute('SELECT * FROM plan_points WHERE plan_id=? ORDER BY at_minute',(pid,)).fetchall()
        body = '''<div class=card><h2>{{plan.name}}</h2><form method=post class=toolbar><input type=hidden name=csrf value='{{csrf}}'><input name=at type=time required><input name=steps type=number min=1 placeholder='固定步数' required><button>添加时间点</button></form><p class=muted>同一时间点仅执行一次；随机偏移由“设置”页统一控制。</p><div class=table-wrap><table class=responsive><thead><tr><th>时间</th><th>固定步数</th><th></th></tr></thead><tbody>{% for p in points %}<tr><td data-label=时间>{{clock(p.at_minute)}}</td><td data-label=固定步数>{{p.low_steps}}</td><td data-label=操作><form class=inline method=post action='{{url_for("delete_point",pid=pid,point_id=p.id)}}'><button class=danger>删除</button></form></td></tr>{% else %}<tr><td colspan=3 class=empty>还没有时间点。</td></tr>{% endfor %}</tbody></table></div><form method=post action='{{url_for("delete_plan",pid=pid)}}'><button class=danger onclick='return confirm("删除计划？已分配账户将变为未分配。")'>删除计划</button></form></div>'''
        return page(body, plan=plan, points=points, pid=pid)
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
        selected = request.args.get('account_id', type=int); cutoff = utcnow(-7 * 24 * 60 * 60)
        with store.conn() as c:
            accounts = c.execute('SELECT id,username,note FROM accounts ORDER BY username').fetchall()
            query, params = 'SELECT * FROM tasks WHERE created_at>=?', [cutoff]
            if selected is not None: query += ' AND account_id=?'; params.append(selected)
            rows = c.execute(query + ' ORDER BY id DESC', params).fetchall()
        body = '''<div class=card><div class=toolbar><h2>执行记录</h2><span class=spacer></span><form method=get><select name=account_id onchange='this.form.submit()'><option value=''>全部账户</option>{% for a in accounts %}<option value={{a.id}} {% if selected==a.id %}selected{% endif %}>{{mask(a.username)}}{% if a.note %} · {{a.note}}{% endif %}</option>{% endfor %}</select><noscript><button class=secondary>筛选</button></noscript></form></div><p class=muted>仅保留最近 7 天的任务记录。</p><div class=table-wrap><table class=responsive><thead><tr><th>时间（{{timezone}}）</th><th>账户</th><th>来源</th><th>目标</th><th>尝试</th><th>状态</th><th>信息</th><th></th></tr></thead><tbody>{% for t in rows %}<tr><td data-label=时间>{{localtime(t.created_at)}}</td><td data-label=账户>{{t.account_label}}</td><td data-label=来源>{{t.trigger}}</td><td data-label=目标>{{t.target_steps or '-'}}</td><td data-label=尝试>{{t.attempts}}</td><td data-label=状态><span class='badge {{t.state}}'>{{state_labels.get(t.state,t.state)}}</span></td><td data-label=信息 class="{{'bad' if t.state=='failed' else ''}}">{{t.error or ''}}</td><td data-label=操作>{% if t.state=='failed' %}<form method=post action='{{url_for("retry_task",tid=t.id)}}'><button class=secondary>重试</button></form>{% endif %}</td></tr>{% else %}<tr><td colspan=8 class=empty>该范围内暂无执行记录。</td></tr>{% endfor %}</tbody></table></div></div>'''
        return page(body, rows=rows, accounts=accounts, selected=selected)
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
