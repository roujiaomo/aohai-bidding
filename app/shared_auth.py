"""Shared local authentication for the isolated Aohai radar and AI-review services.

The module intentionally uses only Python's standard library so it can run on the
existing server without adding a framework or a third-party dependency.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from http.cookies import SimpleCookie
from pathlib import Path


AUTH_DB = Path(os.getenv("AOHAI_AUTH_DB", "/opt/bidding-ai-auth/data/auth.db"))
COOKIE_NAME = "aohai_session"
CSRF_COOKIE_NAME = "aohai_csrf"
SESSION_SECONDS = int(os.getenv("AOHAI_SESSION_SECONDS", "28800"))
IDLE_SECONDS = int(os.getenv("AOHAI_IDLE_SECONDS", "7200"))
COOKIE_SECURE = os.getenv("AOHAI_COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes"}
AUTH_ENABLED = os.getenv("AOHAI_AUTH_ENABLED", "0").strip().lower() in {"1", "true", "yes"}


LOGIN_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>遨海商机雷达 · 登录</title><style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:linear-gradient(135deg,#edf5ff,#f8fbff);font-family:"Microsoft YaHei",system-ui,sans-serif;color:#163a68}.box{width:min(400px,calc(100% - 32px));background:#fff;padding:36px;border-radius:16px;box-shadow:0 16px 40px #1e5b9b22;border:1px solid #dceafb}.brand{font-size:22px;font-weight:700;margin-bottom:8px}.hint{font-size:14px;color:#6c7f99;margin-bottom:28px}label{display:block;font-size:14px;font-weight:600;margin:16px 0 7px}input{width:100%;padding:12px;border:1px solid #cbdced;border-radius:8px;font-size:15px;outline:none}input:focus{border-color:#2681df;box-shadow:0 0 0 3px #2681df1a}button{width:100%;margin-top:22px;padding:12px;border:0;border-radius:8px;background:#2277d8;color:#fff;font-size:15px;font-weight:700;cursor:pointer}button:disabled{opacity:.7;cursor:wait}.err{min-height:20px;margin-top:14px;color:#d64256;font-size:13px}.note{margin-top:22px;font-size:12px;color:#8797ab;line-height:1.6}</style><main class="box"><div class="brand">遨海商机雷达</div><div class="hint">请输入账号和密码后继续访问</div><form id="login"><label>账号</label><input id="username" autocomplete="username" required autofocus><label>密码</label><input id="password" type="password" autocomplete="current-password" required><button id="submit" type="submit">登录</button><div id="err" class="err"></div></form><div class="note">本系统仅限授权人员使用。登录行为会被记录。</div></main><script>document.querySelector('#login').addEventListener('submit',async e=>{e.preventDefault();let b=document.querySelector('#submit'),err=document.querySelector('#err');b.disabled=true;err.textContent='';try{let r=await fetch('api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:username.value,password:password.value})}),j=await r.json();if(!r.ok)throw Error(j.error||'登录失败');location.replace('./')}catch(x){err.textContent=x.message||'账号或密码错误'}finally{b.disabled=false}})</script></html>"""


def _db() -> sqlite3.Connection:
    AUTH_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(AUTH_DB, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    # scrypt is memory-hard and available in the deployed Python standard library.
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$%s$%s" % (salt.hex(), digest.hex())


def _verify_password(password: str, encoded: str) -> bool:
    try:
        kind, n, r, p, salt, digest = encoded.split("$")
        if kind != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(digest)))
        return hmac.compare_digest(actual, bytes.fromhex(digest))
    except (TypeError, ValueError):
        return False


def init_auth() -> None:
    c = _db()
    try:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
          active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL,
          last_login_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS sessions(
          token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, csrf_token TEXT NOT NULL,
          created_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS login_attempts(
          id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, ip TEXT NOT NULL,
          attempted_at INTEGER NOT NULL, success INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
        CREATE INDEX IF NOT EXISTS idx_attempts_user_ip ON login_attempts(username,ip,attempted_at);
        """)
        # A bootstrap account is created only once and only when an explicit
        # password is provided in the protected service environment.
        bootstrap_password = os.getenv("AOHAI_BOOTSTRAP_PASSWORD", "")
        bootstrap_username = os.getenv("AOHAI_BOOTSTRAP_USERNAME", "admin").strip() or "admin"
        if bootstrap_password and c.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None:
            c.execute("INSERT INTO users(username,password_hash,role,active,created_at) VALUES(?,?,?,?,?)",
                      (bootstrap_username, _password_hash(bootstrap_password), "admin", 1, int(time.time())))
        c.execute("DELETE FROM sessions WHERE expires_at<? OR last_seen_at<?", (int(time.time()), int(time.time()) - IDLE_SECONDS))
        c.commit()
    finally:
        c.close()


def auth_enabled() -> bool:
    """登录一期尚未启用域名/HTTPS 时保持系统原有匿名访问行为。"""
    return AUTH_ENABLED


def _cookie(handler, name: str) -> str:
    parsed = SimpleCookie()
    parsed.load(handler.headers.get("Cookie", ""))
    item = parsed.get(name)
    return item.value if item else ""


def current_user(handler) -> dict | None:
    token = _cookie(handler, COOKIE_NAME)
    if not token:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = int(time.time())
    c = _db()
    try:
        row = c.execute("""SELECT u.id,u.username,u.role,u.active,s.csrf_token,s.last_seen_at,s.expires_at
                         FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?""", (digest,)).fetchone()
        if not row or not row["active"] or row["expires_at"] < now or row["last_seen_at"] < now - IDLE_SECONDS:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (digest,)); c.commit(); return None
        c.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (now, digest)); c.commit()
        return dict(row)
    finally:
        c.close()


def csrf_valid(handler, user: dict) -> bool:
    supplied = handler.headers.get("X-CSRF-Token", "")
    cookie = _cookie(handler, CSRF_COOKIE_NAME)
    return bool(supplied and cookie and hmac.compare_digest(supplied, cookie) and hmac.compare_digest(supplied, user["csrf_token"]))


def login(username: str, password: str, ip: str) -> tuple[dict | None, str | None]:
    username = str(username or "").strip()[:100]
    password = str(password or "")
    now = int(time.time())
    c = _db()
    try:
        failures = c.execute("SELECT count(*) FROM login_attempts WHERE username=? AND ip=? AND success=0 AND attempted_at>?", (username, ip, now - 15 * 60)).fetchone()[0]
        if failures >= 5:
            return None, "登录尝试过多，请 15 分钟后再试"
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        ok = bool(row and row["active"] and _verify_password(password, row["password_hash"]))
        c.execute("INSERT INTO login_attempts(username,ip,attempted_at,success) VALUES(?,?,?,?)", (username, ip, now, 1 if ok else 0))
        if not ok:
            c.commit(); return None, "账号或密码错误"
        c.execute("DELETE FROM login_attempts WHERE username=? AND ip=?", (username, ip))
        token = secrets.token_urlsafe(48); csrf = secrets.token_urlsafe(32)
        c.execute("INSERT INTO sessions(token_hash,user_id,csrf_token,created_at,last_seen_at,expires_at) VALUES(?,?,?,?,?,?)",
                  (hashlib.sha256(token.encode()).hexdigest(), row["id"], csrf, now, now, now + SESSION_SECONDS))
        c.execute("UPDATE users SET last_login_at=? WHERE id=?", (now, row["id"])); c.commit()
        return {"id": row["id"], "username": row["username"], "role": row["role"], "token": token, "csrf": csrf}, None
    finally:
        c.close()


def logout(handler) -> None:
    token = _cookie(handler, COOKIE_NAME)
    if not token:
        return
    c = _db()
    try:
        c.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)); c.commit()
    finally:
        c.close()


def session_cookies(user: dict) -> list[str]:
    secure = "; Secure" if COOKIE_SECURE else ""
    lifetime = f"; Max-Age={SESSION_SECONDS}"
    return [
        f"{COOKIE_NAME}={user['token']}; Path=/; HttpOnly; SameSite=Lax{secure}{lifetime}",
        f"{CSRF_COOKIE_NAME}={user['csrf']}; Path=/; SameSite=Lax{secure}{lifetime}",
    ]


def clear_cookies() -> list[str]:
    secure = "; Secure" if COOKIE_SECURE else ""
    return [f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax{secure}; Max-Age=0", f"{CSRF_COOKIE_NAME}=; Path=/; SameSite=Lax{secure}; Max-Age=0"]


def internal_allowed(handler) -> bool:
    expected = os.getenv("AOHAI_INTERNAL_TOKEN", "")
    supplied = handler.headers.get("X-Aohai-Internal-Token", "")
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))
