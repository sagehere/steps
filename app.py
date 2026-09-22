"""Private Zepp Life step scheduler.  Protocol helper derives from mimotion."""
from __future__ import annotations

import base64
import csv
from contextlib import contextmanager
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
from flask import Flask, abort, flash, g, redirect, render_template, render_template_string, request, send_file, session, url_for
import requests
from vendor.util import zepp_helper

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS plans (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1, time_random_enabled INTEGER NOT NULL DEFAULT 0, time_random_min INTEGER NOT NULL DEFAULT -10, time_random_max INTEGER NOT NULL DEFAULT 10, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan_points (id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE, at_minute INTEGER NOT NULL, low_steps INTEGER NOT NULL, high_steps INTEGER NOT NULL, UNIQUE(plan_id, at_minute));
CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, note TEXT NOT NULL DEFAULT '', secret TEXT NOT NULL, token_secret TEXT, plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL, account_label TEXT NOT NULL, point_id INTEGER REFERENCES plan_points(id) ON DELETE SET NULL, run_date TEXT, trigger TEXT NOT NULL, target_steps INTEGER, state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT, created_at TEXT NOT NULL, UNIQUE(account_id, point_id, run_date));
CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), proxy_secret TEXT, random_enabled INTEGER NOT NULL DEFAULT 0, random_min INTEGER NOT NULL DEFAULT -100, random_max INTEGER NOT NULL DEFAULT 100);
CREATE TABLE IF NOT EXISTS daily_plan_offsets (account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE, run_date TEXT NOT NULL, offsets_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(account_id, plan_id, run_date));
CREATE INDEX IF NOT EXISTS task_queue ON tasks(state, available_at, id);
CREATE INDEX IF NOT EXISTS task_created_at ON tasks(created_at);
CREATE TABLE IF NOT EXISTS admin_credentials (token_hash TEXT PRIMARY KEY, expires_at TEXT NOT NULL, auth_version TEXT NOT NULL);
"""

REMEMBER_COOKIE = 'admin_remember'
REMEMBER_SECONDS = 30 * 24 * 60 * 60
STATE_LABELS = {'queued': '排队中', 'running': '执行中', 'success': '成功', 'failed': '失败', 'skipped': '已跳过'}
FALLBACK_DEVICE_ID = 'DA932FFFFE8816E7'
BACKUP_MAGIC = b'STEPSBAK1'
BACKUP_VERSION = 1
BACKUP_LIMIT = 16 * 1024 * 1024

class RestoreBusy(Exception): pass

def today_points(runner, c, account, plan, points, now):
    """One source of truth for the account page and dashboard; never redraw today's offsets."""
    date = now.date().isoformat(); now_min = now.hour * 60 + now.minute
    offsets = runner.daily_offsets(c, account['id'], plan, points, date)
    tasks = {t['point_id']: t for t in c.execute('SELECT * FROM tasks WHERE account_id=? AND run_date=? AND point_id IS NOT NULL', (account['id'], date))}
    active = [p for p in points if p['point_id'] in offsets]
    due = [p for p in active if p['at_minute'] + offsets[p['point_id']] <= now_min]
    latest = max(due, key=lambda p: (p['at_minute'] + offsets[p['point_id']], p['at_minute'], p['point_id'])) if due else None
    rows = []
    for point in active:
        offset = offsets[point['point_id']]; task = tasks.get(point['point_id'])
        if task: status = '重试等待' if task['state'] == 'queued' and task['attempts'] else STATE_LABELS.get(task['state'], task['state'])
        elif not account['enabled'] or not plan['enabled']: status = '不会自动执行'
        elif point['at_minute'] + offset > now_min: status = '待执行'
        elif latest and point['point_id'] == latest['point_id']: status = '待调度'
        else: status = '已错过'
        rows.append({'point_id': point['point_id'], 'at_minute': point['at_minute'], 'offset': f'{offset:+d}', 'final_minute': point['at_minute'] + offset, 'steps': point['low_steps'], 'status': status, 'color': 'green' if task and task['state'] == 'success' else 'red' if task and task['state'] == 'failed' else 'gray'})
    return rows


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
def parse_time_random_range(low, high):
    low, high = parse_random_range(low, high)
    if low < -1439 or high > 1439: raise ValueError("时间随机范围必须在 -1439 至 1439 分钟内")
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

def backup_key(password, salt):
    if not isinstance(password, str) or len(password) < 12: raise ValueError('备份密码至少需要 12 个字符')
    return hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)

def encrypt_backup(payload, password):
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(16)
    cipher = AES.new(backup_key(password, salt), AES.MODE_GCM, nonce=nonce)
    data, tag = cipher.encrypt_and_digest(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode())
    return BACKUP_MAGIC + bytes([BACKUP_VERSION]) + salt + nonce + tag + data

def decrypt_backup(raw, password):
    if not isinstance(raw, bytes) or len(raw) < len(BACKUP_MAGIC) + 49 or not raw.startswith(BACKUP_MAGIC): raise ValueError('备份文件无效或已损坏')
    version = raw[len(BACKUP_MAGIC)]
    if version != BACKUP_VERSION: raise ValueError('不支持的备份文件版本')
    start = len(BACKUP_MAGIC) + 1; salt, nonce, tag, data = raw[start:start + 16], raw[start + 16:start + 32], raw[start + 32:start + 48], raw[start + 48:]
    try:
        cipher = AES.new(backup_key(password, salt), AES.MODE_GCM, nonce=nonce)
        return json.loads(cipher.decrypt_and_verify(data, tag).decode())
    except ValueError: raise ValueError('备份密码错误或文件已损坏')
    except (UnicodeDecodeError, json.JSONDecodeError): raise ValueError('备份文件无效或已损坏')

def validate_backup(payload):
    if not isinstance(payload, dict) or payload.get('version') != BACKUP_VERSION: raise ValueError('不支持的备份内容版本')
    required = {
        'plans': ('id', 'name', 'enabled', 'time_random_enabled', 'time_random_min', 'time_random_max', 'created_at'),
        'plan_points': ('id', 'plan_id', 'at_minute', 'low_steps', 'high_steps'),
        'accounts': ('id', 'username', 'note', 'secret', 'token_secret', 'plan_id', 'enabled', 'created_at', 'updated_at'),
        'tasks': ('id', 'account_id', 'account_label', 'point_id', 'run_date', 'trigger', 'target_steps', 'state', 'attempts', 'available_at', 'started_at', 'finished_at', 'error', 'created_at'),
        'daily_plan_offsets': ('account_id', 'plan_id', 'run_date', 'offsets_json', 'created_at'),
    }
    if not isinstance(payload.get('created_at'), str) or not isinstance(payload.get('timezone'), str): raise ValueError('备份元数据无效')
    for name, keys in required.items():
        rows = payload.get(name)
        if not isinstance(rows, list) or any(not isinstance(row, dict) or set(row) != set(keys) for row in rows): raise ValueError(f'备份中的 {name} 无效')
    settings = payload.get('settings')
    if not isinstance(settings, dict) or set(settings) != {'id', 'proxy_secret', 'random_enabled', 'random_min', 'random_max'}: raise ValueError('备份中的设置无效')
    def identifiers(rows, label):
        ids = [row['id'] for row in rows]
        if any(type(value) is not int or value < 1 for value in ids) or len(ids) != len(set(ids)): raise ValueError(f'备份中的 {label} 编号无效')
        return set(ids)
    plans, points, accounts, tasks = (identifiers(payload[name], name) for name in ('plans', 'plan_points', 'accounts', 'tasks'))
    if len({row['name'] for row in payload['plans']}) != len(plans) or any(not isinstance(row['name'], str) or not row['name'] or row['enabled'] not in (0, 1) or row['time_random_enabled'] not in (0, 1) or not isinstance(row['time_random_min'], int) or not isinstance(row['time_random_max'], int) or row['time_random_min'] > row['time_random_max'] for row in payload['plans']): raise ValueError('备份中的计划无效')
    if any(row['plan_id'] not in plans or not isinstance(row['at_minute'], int) or not 0 <= row['at_minute'] < 1440 or not isinstance(row['low_steps'], int) or row['low_steps'] < 1 or row['high_steps'] != row['low_steps'] for row in payload['plan_points']): raise ValueError('备份中的时间点无效')
    by_plan = {}
    for row in payload['plan_points']: by_plan.setdefault(row['plan_id'], []).append(row)
    if any(len({row['at_minute'] for row in rows}) != len(rows) or any(rows[index]['low_steps'] < rows[index - 1]['low_steps'] for index in range(1, len(rows))) for rows in (sorted(rows, key=lambda row: row['at_minute']) for rows in by_plan.values())): raise ValueError('备份中的时间点无效')
    if len({row['username'] for row in payload['accounts']}) != len(accounts) or any(not isinstance(row['username'], str) or not row['username'] or not isinstance(row['note'], str) or row['enabled'] not in (0, 1) or not isinstance(row['secret'], dict) or not isinstance(row['secret'].get('username'), str) or not isinstance(row['secret'].get('password'), str) or row['plan_id'] is not None and row['plan_id'] not in plans or row['token_secret'] is not None and not isinstance(row['token_secret'], dict) for row in payload['accounts']): raise ValueError('备份中的账户无效')
    if any(row['account_id'] is not None and row['account_id'] not in accounts or row['point_id'] is not None and row['point_id'] not in points or row['state'] not in STATE_LABELS or not isinstance(row['attempts'], int) or row['attempts'] < 0 for row in payload['tasks']): raise ValueError('备份中的任务无效')
    seen_offsets = set()
    for row in payload['daily_plan_offsets']:
        key = (row['account_id'], row['plan_id'], row['run_date'])
        try: offsets = json.loads(row['offsets_json'])
        except (TypeError, json.JSONDecodeError): raise ValueError('备份中的每日偏移无效')
        if key in seen_offsets or row['account_id'] not in accounts or row['plan_id'] not in plans or not isinstance(row['run_date'], str) or not isinstance(offsets, dict): raise ValueError('备份中的每日偏移无效')
        seen_offsets.add(key)
    if settings['id'] != 1 or settings['random_enabled'] not in (0, 1) or not isinstance(settings['random_min'], int) or not isinstance(settings['random_max'], int) or settings['random_min'] > settings['random_max'] or settings['proxy_secret'] is not None and (not isinstance(settings['proxy_secret'], dict) or not isinstance(settings['proxy_secret'].get('url'), str)): raise ValueError('备份中的设置无效')

class Store:
    def __init__(self, path, key):
        self.path, self.key = str(path), key
        self.gate, self.restoring, self.restore_thread = threading.RLock(), threading.Event(), None
    @contextmanager
    def operation(self, exclusive=False):
        if self.restoring.is_set() and not exclusive and self.restore_thread != threading.get_ident(): raise RestoreBusy()
        if not self.gate.acquire(blocking=not exclusive): raise RestoreBusy()
        if exclusive: self.restoring.set(); self.restore_thread = threading.get_ident()
        try: yield
        finally:
            if exclusive: self.restore_thread = None; self.restoring.clear()
            self.gate.release()
    @contextmanager
    def conn(self):
        with self.operation():
            con = sqlite3.connect(self.path, timeout=10, isolation_level=None); con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA foreign_keys=ON")
            try: yield con
            finally: con.close()
    def init(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)
            c.execute('DELETE FROM admin_credentials WHERE expires_at<=?', (utcnow(),))
            columns = {row['name'] for row in c.execute("PRAGMA table_info(plans)")}
            for name, default in (('time_random_enabled', '0'), ('time_random_min', '-10'), ('time_random_max', '10')):
                if name not in columns: c.execute(f"ALTER TABLE plans ADD COLUMN {name} INTEGER NOT NULL DEFAULT {default}")
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
    def backup(self, password, timezone):
        if len(password) < 12: raise ValueError('备份密码至少需要 12 个字符')
        with self.operation():
            with self.conn() as c:
                c.execute('BEGIN')
                payload = {
                    'version': BACKUP_VERSION, 'created_at': utcnow(), 'timezone': timezone,
                    'plans': [dict(row) for row in c.execute('SELECT * FROM plans ORDER BY id')],
                    'plan_points': [dict(row) for row in c.execute('SELECT * FROM plan_points ORDER BY id')],
                    'tasks': [dict(row) for row in c.execute('SELECT * FROM tasks ORDER BY id')],
                    'daily_plan_offsets': [dict(row) for row in c.execute('SELECT * FROM daily_plan_offsets ORDER BY account_id,plan_id,run_date')],
                }
                payload['accounts'] = [{**dict(row), 'secret': self.open(row['secret']), 'token_secret': self.open(row['token_secret']) if row['token_secret'] else None} for row in c.execute('SELECT * FROM accounts ORDER BY id')]
                settings = dict(c.execute('SELECT * FROM settings WHERE id=1').fetchone())
                payload['settings'] = {**settings, 'proxy_secret': self.open(settings['proxy_secret']) if settings['proxy_secret'] else None}
                c.commit()
        return encrypt_backup(payload, password)
    def restore(self, raw, password):
        payload = decrypt_backup(raw, password)
        validate_backup(payload)
        with self.operation(exclusive=True):
            with self.conn() as c:
                c.execute('BEGIN IMMEDIATE')
                for table in ('tasks', 'daily_plan_offsets', 'accounts', 'plan_points', 'plans', 'settings'):
                    c.execute(f'DELETE FROM {table}')
                for row in payload['plans']: c.execute('INSERT INTO plans(id,name,enabled,time_random_enabled,time_random_min,time_random_max,created_at) VALUES(:id,:name,:enabled,:time_random_enabled,:time_random_min,:time_random_max,:created_at)', row)
                for row in payload['plan_points']: c.execute('INSERT INTO plan_points(id,plan_id,at_minute,low_steps,high_steps) VALUES(:id,:plan_id,:at_minute,:low_steps,:high_steps)', row)
                for row in payload['accounts']:
                    row = {**row, 'secret': self.seal(row['secret']), 'token_secret': self.seal(row['token_secret']) if row['token_secret'] else None, 'enabled': 0}
                    c.execute('INSERT INTO accounts(id,username,note,secret,token_secret,plan_id,enabled,created_at,updated_at) VALUES(:id,:username,:note,:secret,:token_secret,:plan_id,:enabled,:created_at,:updated_at)', row)
                for row in payload['tasks']:
                    row = {**row}
                    if row['state'] in ('queued', 'running'):
                        row.update(state='skipped', finished_at=utcnow(), error='备份恢复后暂停，未自动重放', started_at=None)
                    c.execute('INSERT INTO tasks(id,account_id,account_label,point_id,run_date,trigger,target_steps,state,attempts,available_at,started_at,finished_at,error,created_at) VALUES(:id,:account_id,:account_label,:point_id,:run_date,:trigger,:target_steps,:state,:attempts,:available_at,:started_at,:finished_at,:error,:created_at)', row)
                for row in payload['daily_plan_offsets']: c.execute('INSERT INTO daily_plan_offsets(account_id,plan_id,run_date,offsets_json,created_at) VALUES(:account_id,:plan_id,:run_date,:offsets_json,:created_at)', row)
                settings = {**payload['settings'], 'id': 1, 'proxy_secret': self.seal(payload['settings']['proxy_secret']) if payload['settings']['proxy_secret'] else None}
                c.execute('INSERT INTO settings(id,proxy_secret,random_enabled,random_min,random_max) VALUES(:id,:proxy_secret,:random_enabled,:random_min,:random_max)', settings)
                c.commit()
        return {name: len(payload[name]) for name in ('accounts', 'plans', 'tasks')}

class Runner:
    def __init__(self, store, tz, delay): self.store, self.tz, self.delay = store, ZoneInfo(tz), delay
    def client(self, proxy=None):
        proxy = self.store.proxy_url() if proxy is None else proxy
        client = requests.Session(); client.trust_env = False
        if proxy: client.proxies.update({'http': proxy, 'https': proxy})
        return client
    def now(self): return datetime.now(self.tz)
    def target(self, base, settings):
        if settings['random_enabled']: return base + random.randint(settings['random_min'], settings['random_max'])
        return base
    def valid_base(self, base, settings):
        if base < 1: raise ValueError("固定步数必须为正整数")
        if settings['random_enabled'] and base + settings['random_min'] < 1: raise ValueError("固定步数加随机下限必须至少为 1")
    def valid_time_random(self, minutes, enabled, low, high):
        if enabled and any(at + low < 0 or at + high > 1439 for at in minutes): raise ValueError("随机时间范围会使已有时间点跨天")
    def daily_offsets(self, c, account_id, plan, points, date):
        row = c.execute("SELECT offsets_json FROM daily_plan_offsets WHERE account_id=? AND plan_id=? AND run_date=?", (account_id, plan['id'], date)).fetchone()
        if not row:
            offsets = {str(point['point_id']): random.randint(plan['time_random_min'], plan['time_random_max']) if plan['time_random_enabled'] else 0 for point in points}
            c.execute("INSERT OR IGNORE INTO daily_plan_offsets(account_id,plan_id,run_date,offsets_json,created_at) VALUES(?,?,?,?,?)", (account_id, plan['id'], date, json.dumps(offsets, separators=(',', ':')), utcnow()))
            row = c.execute("SELECT offsets_json FROM daily_plan_offsets WHERE account_id=? AND plan_id=? AND run_date=?", (account_id, plan['id'], date)).fetchone()
        return {int(point_id): int(offset) for point_id, offset in json.loads(row['offsets_json']).items()}
    def add_task(self, c, account, base, settings, trigger, point_id=None, run_date=None):
        self.valid_base(base, settings); target = self.target(base, settings)
        previous = c.execute("SELECT MAX(target_steps) FROM tasks WHERE account_id=? AND run_date=? AND state='success'", (account['id'], run_date)).fetchone()[0] if run_date else None
        skipped = previous is not None and target < previous
        values = (account['id'], mask(account['username']), point_id, run_date, trigger, target, 'skipped' if skipped else 'queued', utcnow(), utcnow() if skipped else None, '目标低于当天已成功步数，已跳过' if skipped else None, utcnow())
        sql = "INSERT {} INTO tasks(account_id,account_label,point_id,run_date,trigger,target_steps,state,available_at,finished_at,error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)".format("OR IGNORE" if point_id else "")
        changed = c.execute(sql, values).rowcount
        return ('skipped' if skipped else 'queued') if changed else None
    def enqueue_scheduled(self):
        now = self.now(); date, now_min = now.date().isoformat(), now.hour * 60 + now.minute
        settings = self.store.settings()
        with self.store.conn() as c:
            c.execute("DELETE FROM daily_plan_offsets WHERE run_date<?", (date,))
            rows = c.execute("""SELECT a.*, pl.id schedule_plan_id,pl.time_random_enabled,pl.time_random_min,pl.time_random_max,p.id point_id,p.at_minute,p.low_steps,p.high_steps FROM accounts a JOIN plans pl ON pl.id=a.plan_id AND pl.enabled=1 JOIN plan_points p ON p.plan_id=pl.id WHERE a.enabled=1 ORDER BY a.id,p.at_minute,p.id""").fetchall()
            grouped = {}
            for row in rows: grouped.setdefault(row['id'], []).append(row)
            for account_id, points in grouped.items():
                plan = {'id': points[0]['schedule_plan_id'], 'time_random_enabled': points[0]['time_random_enabled'], 'time_random_min': points[0]['time_random_min'], 'time_random_max': points[0]['time_random_max']}
                offsets = self.daily_offsets(c, account_id, plan, points, date)
                due = [point for point in points if point['point_id'] in offsets and point['at_minute'] + offsets[point['point_id']] <= now_min]
                if due:
                    point = max(due, key=lambda value: (value['at_minute'] + offsets[value['point_id']], value['at_minute'], value['point_id']))
                    self.add_task(c, point, point['low_steps'], settings, '计划', point['point_id'], date)
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
    def save_tokens(self, account_id, tokens):
        with self.store.conn() as c: c.execute("UPDATE accounts SET token_secret=?,updated_at=? WHERE id=?", (self.store.seal(tokens), utcnow(), account_id))
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
        self.save_tokens(account['id'], tokens)
        return app_token, uid, tokens
    def device_for_upload(self, account, client):
        app_token, uid, tokens = self.authenticate(account, client)
        try:
            devices = zepp_helper.get_device_list(app_token, uid, client=client)
        except ValueError as exc:
            return app_token, uid, FALLBACK_DEVICE_ID, "设备查询失败，已使用 fallback device：" + str(exc)
        if devices:
            device_id = devices[0].get('deviceid')
            if not device_id: raise ValueError("设备列表响应无效：缺少设备 ID")
            tokens['bound_device_id'] = str(device_id).replace(':', '').upper()
            self.save_tokens(account['id'], tokens)
            return app_token, uid, tokens['bound_device_id'], None
        device_id = tokens.get('virtual_device_id')
        device_mac = tokens.get('virtual_device_mac')
        if not device_id or not device_mac:
            device_id = secrets.token_hex(8).upper()
            device_mac = ':'.join(secrets.token_hex(6).upper()[index:index + 2] for index in range(0, 12, 2))
            tokens.update(virtual_device_id=device_id, virtual_device_mac=device_mac)
            self.save_tokens(account['id'], tokens)
        try:
            zepp_helper.bind_virtual_device(app_token, uid, device_mac, device_id, client=client)
        except ValueError as exc:
            return app_token, uid, FALLBACK_DEVICE_ID, "自动绑定失败，已使用 fallback device：" + str(exc)
        tokens['bound_device_id'] = device_id
        self.save_tokens(account['id'], tokens)
        return app_token, uid, device_id, None
    def execute(self, task):
        with self.store.conn() as c: account = c.execute("SELECT * FROM accounts WHERE id=?", (task['account_id'],)).fetchone()
        if not account: return False, "账户已删除", False
        try:
            client = self.client()
            if task['trigger'] == '测试':
                app_token, uid, _ = self.authenticate(account, client)
                devices = zepp_helper.get_device_list(app_token, uid, client=client)
                return True, "登录和设备检查成功" if devices else "登录成功，当前账户尚无绑定设备", False
            app_token, uid, device, binding_note = self.device_for_upload(account, client)
            ok, message = zepp_helper.post_fake_brand_data(str(task['target_steps']), app_token, uid, device, client=client)
            detail = binding_note or ("提交成功" if ok else "提交失败：" + str(message))
            return ok, detail, not ok and any(x in str(message) for x in ('429', '500', '502', '503', '504'))
        except Exception as exc:
            message = safe_error(exc, self.store.proxy_url() or ''); return False, message, not message.startswith('认证失败')
    def work_once(self):
        with self.store.operation():
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
    app = Flask(__name__); app.config.update(SECRET_KEY=hmac.new(secret.encode(), b'session-key', hashlib.sha256).digest(), DB=config.get('DB', os.getenv('DATABASE_PATH', '/data/app.db')), ADMIN_PASSWORD=config.get('ADMIN_PASSWORD', os.getenv('ADMIN_PASSWORD', 'admin')), TZ=config.get('TZ', os.getenv('TZ', 'Asia/Shanghai')), DELAY=float(config.get('DELAY', os.getenv('REQUEST_INTERVAL_SECONDS', '5'))), COOKIE_SECURE=str(config.get('COOKIE_SECURE', os.getenv('COOKIE_SECURE', 'true'))).lower() == 'true', START_WORKER=config.get('START_WORKER', True), MAX_CONTENT_LENGTH=BACKUP_LIMIT + 128 * 1024)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=app.config['COOKIE_SECURE'])
    store, runner = Store(app.config['DB'], key), None; store.init(); runner = Runner(store, app.config['TZ'], app.config['DELAY']); app.extensions['store'], app.extensions['runner'] = store, runner
    attempts = {}
    def auth_version():
        return hmac.new(app.config['SECRET_KEY'], app.config['ADMIN_PASSWORD'].encode(), hashlib.sha256).hexdigest()
    def token_hash(token): return hashlib.sha256(token.encode()).hexdigest()
    def revoke_remember(c):
        token = request.cookies.get(REMEMBER_COOKIE)
        if token: c.execute('DELETE FROM admin_credentials WHERE token_hash=?', (token_hash(token),))
    def issue_remember(c):
        token = secrets.token_urlsafe(32)
        c.execute('INSERT INTO admin_credentials VALUES(?,?,?)', (token_hash(token), utcnow(REMEMBER_SECONDS), auth_version()))
        g.remember_token = token
    def start_session():
        session.clear(); session['admin'] = True; session['auth_version'] = auth_version(); csrf()
    @app.before_request
    def restore_admin():
        if store.restoring.is_set() and request.endpoint not in ('health', 'static', 'import_backup'): abort(503, '正在恢复备份，请稍后重试')
        if session.get('admin') and session.get('auth_version') != auth_version(): session.clear()
        if session.get('admin') or request.endpoint in ('static', 'health', 'logout') or (request.endpoint == 'login' and request.method == 'POST'): return
        token = request.cookies.get(REMEMBER_COOKIE)
        if not token or len(token) > 128: return
        with store.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM admin_credentials WHERE expires_at<=? OR auth_version<>?', (utcnow(), auth_version()))
            removed = c.execute('DELETE FROM admin_credentials WHERE token_hash=?', (token_hash(token),)).rowcount
            if removed: issue_remember(c)
            c.commit()
        if removed: start_session()
    @app.errorhandler(413)
    def too_large(_): return '备份文件超过 16 MiB 限制', 413
    @app.after_request
    def remember_response(response):
        if hasattr(g, 'remember_token'):
            if g.remember_token:
                response.set_cookie(REMEMBER_COOKIE, g.remember_token, max_age=REMEMBER_SECONDS, httponly=True, secure=app.config['COOKIE_SECURE'], samesite='Lax')
            else: response.delete_cookie(REMEMBER_COOKIE, httponly=True, secure=app.config['COOKIE_SECURE'], samesite='Lax')
        if request.endpoint != 'static': response.headers['Cache-Control'] = 'no-store'
        return response
    def csrf():
        if 'csrf' not in session: session['csrf'] = secrets.token_urlsafe(24)
        return session['csrf']
    @app.context_processor
    def inject(): return {'csrf': csrf(), 'clock': clock, 'mask': mask, 'localtime': lambda value: localtime(value, app.config['TZ']), 'timezone': app.config['TZ'], 'state_labels': STATE_LABELS}
    @app.before_request
    def verify_csrf():
        if request.method == 'POST' and request.endpoint not in ('login', 'health') and (not session.get('csrf') or request.form.get('csrf') != session.get('csrf')): abort(400, 'CSRF 校验失败')
    def page(body, template=False, **kw):
        rendered = render_template(body, **kw) if template else render_template_string(body, **kw)
        rendered = re.sub(r'(<form\b[^>]*\bmethod=post[^>]*>)', r'\1<input type=hidden name=csrf value="' + csrf() + '">', rendered, flags=re.I)
        return render_template('layout.html', body=rendered)
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
            elif hmac.compare_digest(request.form.get('password',''), app.config['ADMIN_PASSWORD']):
                with store.conn() as c:
                    c.execute('BEGIN IMMEDIATE'); revoke_remember(c)
                    c.execute('DELETE FROM admin_credentials WHERE expires_at<=? OR auth_version<>?', (utcnow(), auth_version()))
                    g.remember_token = None
                    if request.form.get('remember') == '1': issue_remember(c)
                    c.commit()
                start_session(); return redirect(url_for('dashboard'))
            else: attempts[ip].append(now); flash('密码错误')
        if session.get('admin'): return redirect(url_for('dashboard'))
        return page("<div class='card login-card'><span class=eyebrow>ZEPP LIFE · 管理中心</span><h2>欢迎回来</h2><p class=muted>登录后，管理账户与每日步数计划。</p><form method=post><label class=field>管理员密码<input type=password name=password autocomplete=current-password autofocus required placeholder='请输入管理员密码'></label><label class=remember><input type=checkbox name=remember value=1> 记住我（30 天）</label><button class=login-button>登录管理中心 <span aria-hidden=true>→</span></button></form><p class='muted login-hint'>仅在您信任的设备上启用记住我</p></div>")
    @app.post('/logout')
    def logout():
        with store.conn() as c: revoke_remember(c)
        g.remember_token = None; session.clear(); return redirect(url_for('login'))
    @app.get('/')
    @required
    def dashboard():
        local_midnight = runner.now().replace(hour=0, minute=0, second=0, microsecond=0)
        day_start = local_midnight.astimezone(UTC).replace(tzinfo=None).isoformat()
        day_end = (local_midnight + timedelta(days=1)).astimezone(UTC).replace(tzinfo=None).isoformat()
        with store.conn() as c:
            stats=c.execute("SELECT (SELECT count(*) FROM accounts WHERE enabled=1) accounts,(SELECT count(*) FROM plans WHERE enabled=1) plans,(SELECT count(*) FROM tasks WHERE state='queued') queued,(SELECT count(*) FROM tasks WHERE state='failed' AND finished_at>=? AND finished_at<?) failed", (day_start, day_end)).fetchone(); recent=c.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 12").fetchall()
        return page('dashboard.html', template=True, stats=dict(stats), recent=recent)
    @app.get('/dashboard/timeline')
    @required
    def dashboard_timeline():
        selected = request.args.get('plan_id', type=int)
        if request.args.get('plan_id') and selected is None: abort(400)
        now = runner.now(); dots = []
        with store.conn() as c:
            plans = [dict(p) for p in c.execute('SELECT * FROM plans ORDER BY name,id')]
            if selected is not None and not any(p['id'] == selected for p in plans): abort(404)
            accounts = c.execute('SELECT id,username,note,enabled,plan_id FROM accounts WHERE plan_id IS NOT NULL ORDER BY id').fetchall()
            points = c.execute('SELECT id point_id,plan_id,at_minute,low_steps FROM plan_points ORDER BY at_minute,id').fetchall()
            by_plan = {}
            for point in points: by_plan.setdefault(point['plan_id'], []).append(point)
            plan_map = {p['id']: p for p in plans}
            for account in accounts:
                if selected is not None and account['plan_id'] != selected: continue
                plan = plan_map[account['plan_id']]
                for row in today_points(runner, c, account, plan, by_plan.get(plan['id'], []), now):
                    dots.append(dict(row, id=f"{now.date()}:{account['id']}:{row['point_id']}", account=mask(account['username']), note=account['note'], plan_id=plan['id'], plan_name=plan['name']))
        return {'date': now.date().isoformat(), 'timezone': app.config['TZ'], 'now_minute': now.hour * 60 + now.minute + now.second / 60, 'plans': [{'id': p['id'], 'name': p['name']} for p in plans], 'points': dots}
    @app.get('/settings')
    @required
    def settings_page():
        values = store.settings()
        body = '''<div class=card><h2>SOCKS5 代理</h2><p class=muted>当前：{{proxy}}</p><form method=post action='{{url_for("save_proxy")}}' class=toolbar><label class='field grow'>代理地址<input name=proxy_url required placeholder='socks5h://[用户名:密码@]主机:端口'></label><button>保存代理</button></form><form class=inline method=post action='{{url_for("test_proxy")}}'><button class=secondary>验证已保存代理</button></form> <form class=inline method=post action='{{url_for("clear_proxy")}}'><button class=danger onclick='return confirm("清除代理设置？")'>清除代理</button></form><p class=muted>推荐 socks5h：域名解析也经代理进行。</p></div><div class=card><h2>随机步数偏移</h2><form method=post action='{{url_for("save_random")}}' class=toolbar><label><input type=checkbox name=enabled value=1 {% if values.random_enabled %}checked{% endif %}> 启用随机</label><label class=field>随机下限<input type=number name=low value='{{values.random_min}}' required placeholder='下限'></label><label class=field>随机上限<input type=number name=high value='{{values.random_max}}' required placeholder='上限'></label><button>保存随机设置</button></form><p class=muted>启用后，每个新任务的目标为固定步数加上此范围内重新抽取的随机数。</p></div><div class=card><h2>灾难恢复备份</h2><p class=muted>备份包含账户凭据、计划、设置和执行记录；请将备份密码单独保管。</p><form method=post action='{{url_for("export_backup")}}' class=toolbar><label class=field>备份密码<input type=password name=password minlength=12 required autocomplete=new-password></label><label class=field>确认密码<input type=password name=confirmation minlength=12 required autocomplete=new-password></label><button>导出加密备份</button></form><hr><p class=muted>导入会完整覆盖当前业务数据。恢复的账户将全部停用，未完成任务不会自动重放。</p><form method=post action='{{url_for("import_backup")}}' enctype=multipart/form-data class=toolbar><label class=field>备份文件<input type=file name=backup accept='.stepsbak,application/octet-stream' required></label><label class=field>备份密码<input type=password name=password required autocomplete=current-password></label><label><input type=checkbox name=confirm value=overwrite required> 我确认覆盖当前全部业务数据</label><button class=danger>导入并恢复</button></form></div>'''
        return page(body, values=values, proxy=mask_proxy(store.proxy_url()))
    @app.post('/settings/backup/export')
    @required
    def export_backup():
        try:
            if request.form.get('password') != request.form.get('confirmation'): raise ValueError('两次输入的备份密码不一致')
            data = store.backup(request.form.get('password', ''), app.config['TZ'])
            return send_file(io.BytesIO(data), mimetype='application/octet-stream', as_attachment=True, download_name=f"steps-{datetime.now().date().isoformat()}.stepsbak")
        except RestoreBusy: flash('系统正在处理其他任务，请稍后重试')
        except Exception as e: flash('导出备份失败：' + safe_error(e))
        return redirect(url_for('settings_page'))
    @app.post('/settings/backup/import')
    @required
    def import_backup():
        try:
            if request.form.get('confirm') != 'overwrite': raise ValueError('请确认覆盖当前全部业务数据')
            uploaded = request.files.get('backup')
            if not uploaded or not uploaded.filename: raise ValueError('请选择备份文件')
            raw = uploaded.read(BACKUP_LIMIT + 1)
            if len(raw) > BACKUP_LIMIT: raise ValueError('备份文件超过 16 MiB 限制')
            counts = store.restore(raw, request.form.get('password', ''))
            flash(f"恢复成功：{counts['accounts']} 个账户、{counts['plans']} 个计划、{counts['tasks']} 条记录。请核对设置后再启用账户。")
        except RestoreBusy: flash('系统正在处理其他任务，请稍后重试')
        except Exception as e: flash('恢复失败：' + safe_error(e))
        return redirect(url_for('settings_page'))
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
        body = '''<div class=card><h2>添加账户</h2><form method=post class=grid><input type=hidden name=csrf value='{{csrf}}'><label class=field>Zepp Life 账号<input name=username placeholder='手机号或邮箱' autocomplete=username required></label><label class=field>账号密码<input name=password type=password placeholder=密码 autocomplete=new-password required></label><label class=field>账号备注<input name=note placeholder=备注></label><label class=field>分配计划<select name=plan_id><option value=''>不分配计划</option>{% for p in plans %}<option value={{p.id}}>{{p.name}}</option>{% endfor %}</select></label><button>添加</button></form></div><div class=card><h2>批量导入</h2><form method=post action='{{url_for("import_accounts")}}'><label class=field>分配计划<select name=plan_id><option value=''>不分配计划</option>{% for p in plans %}<option value={{p.id}}>{{p.name}}</option>{% endfor %}</select></label><label class=field>导入内容<textarea name=rows placeholder='每行：账号,密码,备注'></textarea></label><button>导入</button></form></div><div class=card><h2>账户</h2><form method=post action='{{url_for("manual_run")}}'><div class=toolbar><label><input id=select-all type=checkbox> 全选</label><label class=field>分配计划<select name=plan_id><option value=''>取消计划分配</option>{% for p in plans %}<option value={{p.id}}>{{p.name}}</option>{% endfor %}</select></label><button class=secondary formnovalidate formaction='{{url_for("bulk_assign_plan")}}'>批量配置计划</button><button class=secondary formnovalidate formaction='{{url_for("bulk_enable_accounts")}}'>批量启用</button></div><div class=table-wrap><table class=responsive><thead><tr><th></th><th>账号</th><th>备注/计划</th><th>状态</th><th>操作</th></tr></thead><tbody>{% for a in rows %}<tr><td data-label=选择><input aria-label="选择 {{mask(a.username)}}" class=account-select type=checkbox name=account_id value={{a.id}}></td><td data-label=账号>{{mask(a.username)}}</td><td data-label=备注/计划>{{a.note}}<br><span class=muted>{{a.plan_name or '未分配'}}</span></td><td data-label=状态><span class=badge>{{'启用' if a.enabled else '停用'}}</span></td><td data-label=操作 class=actions><a class=link href='{{url_for("account_today",aid=a.id)}}'>今日计划</a> <a class=link href='{{url_for("edit_account",aid=a.id)}}'>编辑</a> <button formnovalidate formaction='{{url_for("test_account",aid=a.id)}}'>测试</button><button class=secondary formnovalidate formaction='{{url_for("toggle_account",aid=a.id)}}'>{{'停用' if a.enabled else '启用'}}</button><button class=danger formnovalidate formaction='{{url_for("delete_account",aid=a.id)}}' onclick='return confirm("删除账户及其凭据？")'>删除</button></td></tr>{% else %}<tr><td colspan=5 class=empty>还没有账户，先添加或批量导入。</td></tr>{% endfor %}</tbody></table></div><div class=toolbar><span class=muted>手动执行仅处理启用账户。</span><span class=spacer></span><label class=field>固定步数<input name=steps type=number min=1 required placeholder=固定步数></label><button>执行选中账户</button></div></form></div><script>document.getElementById('select-all')?.addEventListener('change',function(){document.querySelectorAll('.account-select').forEach(function(box){box.checked=this.checked},this)})</script>'''
        return page(body, rows=rows, plans=plans)
    @app.get('/accounts/<int:aid>/today')
    @required
    def account_today(aid):
        now = runner.now()
        with store.conn() as c:
            account = c.execute('SELECT * FROM accounts WHERE id=?', (aid,)).fetchone()
            if not account: abort(404)
            plan = c.execute('SELECT * FROM plans WHERE id=?', (account['plan_id'],)).fetchone() if account['plan_id'] else None
            points = c.execute('SELECT id point_id,at_minute,low_steps FROM plan_points WHERE plan_id=? ORDER BY at_minute,id', (plan['id'],)).fetchall() if plan else []
            rows = today_points(runner, c, account, plan, points, now) if plan else []
        body = '''<div class=card><h2>{{mask(account.username)}} 的今日计划</h2>{% if not plan %}<p class=empty>该账户未分配计划。</p>{% elif not points %}<p class=empty>计划“{{plan.name}}”还没有时间点。</p>{% else %}<p class=muted>计划：{{plan.name}} · 账户{{'启用' if account.enabled else '停用'}} · 计划{{'启用' if plan.enabled else '停用'}}。今日偏移已固定。</p><div class=table-wrap><table class=responsive><thead><tr><th>原时间</th><th>偏移（分钟）</th><th>最终时间</th><th>固定步数</th><th>状态</th></tr></thead><tbody>{% for row in rows %}<tr><td data-label=原时间>{{clock(row.at_minute)}}</td><td data-label=偏移>{{row.offset}}</td><td data-label=最终时间>{{clock(row.final_minute)}}</td><td data-label=固定步数>{{row.steps}}</td><td data-label=状态>{{row.status}}</td></tr>{% else %}<tr><td colspan=5 class=empty>今日快照创建后新增的时间点将从明日起生效。</td></tr>{% endfor %}</tbody></table></div>{% endif %}<p><a class=link href='{{url_for("accounts")}}'>返回账户</a></p></div>'''
        return page(body, account=account, plan=plan, points=points, rows=rows)
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
        return page('''<div class=card><h2>编辑 {{mask(account.username)}}</h2><form method=post><label class=field>备注 <input name=note value='{{account.note}}'></label><label class=field>新密码（留空则保持不变） <input type=password name=password></label><label class=field>分配计划<select name=plan_id><option value=''>不分配计划</option>{% for p in plans %}<option value={{p.id}} {% if account.plan_id==p.id %}selected{% endif %}>{{p.name}}</option>{% endfor %}</select></label><button>保存</button></form></div>''', account=account, plans=plans)
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
    @app.post('/accounts/enable')
    @required
    def bulk_enable_accounts():
        ids = request.form.getlist('account_id')
        try:
            if not ids: raise ValueError('请至少选择一个账户')
            marks = ','.join('?' for _ in ids)
            with store.conn() as c: count = c.execute(f'UPDATE accounts SET enabled=1,updated_at=? WHERE id IN ({marks})', (utcnow(), *ids)).rowcount
            flash(f'已启用 {count} 个账户')
        except Exception as e: flash('批量启用失败：' + str(e))
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
        with store.conn() as c: rows=c.execute("SELECT p.*,count(DISTINCT a.id) accounts,count(DISTINCT pp.id) points FROM plans p LEFT JOIN accounts a ON a.plan_id=p.id LEFT JOIN plan_points pp ON pp.plan_id=p.id GROUP BY p.id ORDER BY p.name").fetchall()
        body = '''<div class=card><h2>新建计划</h2><form method=post><input type=hidden name=csrf value='{{csrf}}'><label class=field>计划名称<input name=name required placeholder='例如：日常运动'></label><button>创建</button></form></div><div class=card><div class=table-wrap><table class=responsive><thead><tr><th>名称</th><th>账户</th><th>时间点</th><th>状态</th><th></th></tr></thead><tbody>{% for p in rows %}<tr><td data-label=名称>{{p.name}}</td><td data-label=账户>{{p.accounts}}</td><td data-label=时间点>{{p.points}}</td><td data-label=状态><span class=badge>{{'启用' if p.enabled else '停用'}}</span></td><td data-label=操作><a class=link href='{{url_for("plan_detail",pid=p.id)}}'>编辑</a> <form class=inline method=post action='{{url_for("delete_plan",pid=p.id)}}'><button class=danger onclick='return confirm("删除计划？已分配账户将变为未分配。")'>删除</button></form></td></tr>{% else %}<tr><td colspan=5 class=empty>还没有计划。</td></tr>{% endfor %}</tbody></table></div></div>'''
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
                runner.valid_time_random([at], plan['time_random_enabled'], plan['time_random_min'], plan['time_random_max'])
                with store.conn() as c:
                    points=c.execute('SELECT * FROM plan_points WHERE plan_id=? ORDER BY at_minute',(pid,)).fetchall()
                    candidate=[(p['at_minute'],p['low_steps']) for p in points]+[(at,steps)]; candidate.sort()
                    if any(candidate[i][1] < candidate[i-1][1] for i in range(1,len(candidate))): raise ValueError('时间点固定步数必须单调递增')
                    c.execute('INSERT INTO plan_points(plan_id,at_minute,low_steps,high_steps) VALUES(?,?,?,?)',(pid,at,steps,steps))
                flash('时间点已添加')
            except Exception as e: flash('添加失败：'+str(e))
            return redirect(url_for('plan_detail',pid=pid))
        with store.conn() as c: points=c.execute('SELECT * FROM plan_points WHERE plan_id=? ORDER BY at_minute',(pid,)).fetchall()
        body = '''<div class=card><h2>{{plan.name}}</h2><form method=post class=toolbar><input type=hidden name=csrf value='{{csrf}}'><label class=field>执行时间<input name=at type=time required></label><label class=field>固定步数<input name=steps type=number min=1 placeholder='固定步数' required></label><button>添加时间点</button></form><p class=muted>同一时间点仅执行一次。</p><div class=table-wrap><table class=responsive><thead><tr><th>时间</th><th>固定步数</th><th></th></tr></thead><tbody>{% for p in points %}<tr><td data-label=时间>{{clock(p.at_minute)}}</td><td data-label=固定步数>{{p.low_steps}}</td><td data-label=操作><form class=inline method=post action='{{url_for("delete_point",pid=pid,point_id=p.id)}}'><button class=danger>删除</button></form></td></tr>{% else %}<tr><td colspan=3 class=empty>还没有时间点。</td></tr>{% endfor %}</tbody></table></div><form method=post action='{{url_for("save_plan_time_random",pid=pid)}}' class=toolbar><label><input type=checkbox name=enabled value=1 {% if plan.time_random_enabled %}checked{% endif %}> 启用随机时间</label><label class=field>随机下限<input type=number name=low value='{{plan.time_random_min}}' required placeholder='下限（分钟）'></label><label class=field>随机上限<input type=number name=high value='{{plan.time_random_max}}' required placeholder='上限（分钟）'></label><button>保存随机时间</button></form><p class=muted>开启后，每个账户每天首次调度或查看今日计划时，按时间点顺序独立抽取偏移；当天结果固定。偏移不能跨天。</p><form method=post action='{{url_for("delete_plan",pid=pid)}}'><button class=danger onclick='return confirm("删除计划？已分配账户将变为未分配。")'>删除计划</button></form></div>'''
        return page(body, plan=plan, points=points, pid=pid)
    @app.post('/plans/<int:pid>/time-random')
    @required
    def save_plan_time_random(pid):
        try:
            enabled = bool(request.form.get('enabled'))
            low, high = parse_time_random_range(request.form['low'], request.form['high'])
            with store.conn() as c:
                plan = c.execute('SELECT * FROM plans WHERE id=?', (pid,)).fetchone()
                if not plan: abort(404)
                points = c.execute('SELECT at_minute FROM plan_points WHERE plan_id=?', (pid,)).fetchall()
                runner.valid_time_random([point['at_minute'] for point in points], enabled, low, high)
                c.execute('UPDATE plans SET time_random_enabled=?,time_random_min=?,time_random_max=? WHERE id=?', (int(enabled), low, high, pid))
            flash('随机时间设置已保存，将从该账户当天首次调度或查看时生效')
        except Exception as e: flash('随机时间设置保存失败：' + str(e))
        return redirect(url_for('plan_detail', pid=pid))
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
        body = '''<div class=card><div class=toolbar><h2>执行记录</h2><span class=spacer></span><form method=get><select aria-label=筛选账户 name=account_id onchange='this.form.submit()'><option value=''>全部账户</option>{% for a in accounts %}<option value={{a.id}} {% if selected==a.id %}selected{% endif %}>{{mask(a.username)}}{% if a.note %} · {{a.note}}{% endif %}</option>{% endfor %}</select><noscript><button class=secondary>筛选</button></noscript></form></div><p class=muted>仅保留最近 7 天的任务记录。</p><div class=table-wrap><table class=responsive><thead><tr><th>时间（{{timezone}}）</th><th>账户</th><th>来源</th><th>目标</th><th>尝试</th><th>状态</th><th>信息</th><th></th></tr></thead><tbody>{% for t in rows %}<tr><td data-label=时间>{{localtime(t.created_at)}}</td><td data-label=账户>{{t.account_label}}</td><td data-label=来源>{{t.trigger}}</td><td data-label=目标>{{t.target_steps or '-'}}</td><td data-label=尝试>{{t.attempts}}</td><td data-label=状态><span class='badge {{t.state}}'>{{state_labels.get(t.state,t.state)}}</span></td><td data-label=信息 class="{{'bad' if t.state=='failed' else ''}}">{{t.error or ''}}</td><td data-label=操作>{% if t.state=='failed' %}<form method=post action='{{url_for("retry_task",tid=t.id)}}'><button class=secondary>重试</button></form>{% endif %}</td></tr>{% else %}<tr><td colspan=8 class=empty>该范围内暂无执行记录。</td></tr>{% endfor %}</tbody></table></div></div>'''
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

