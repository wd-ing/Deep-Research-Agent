"""小范围多人使用的鉴权核心（坑 99·段 1）。

设计要点：
- 用户库 `RA_USERS_PATH`（默认 `data/users.json`），**只存 token 的
  sha256**——文件泄露不暴露邀请链接。写文件用「临时文件 + rename」
  原子写，写完 chmod 600。
- cookie = `uid|签发时间|随机数`，签名 = HMAC-SHA256(secret, 内容)，
  校验走 `hmac.compare_digest` 常量时间比对。密钥取 `RA_AUTH_SECRET`
  或 `data/auth_secret`（首次自动生成，权限 600）。
- `load_users()` 每次读文件 mtime——`disable`/`rotate` 写库后即时生效。
- `RA_AUTH` 调用时读：缺省=开；`=0` 时 `LOCAL_ADMIN` 固定身份通行
  （本地开发与全部既有测试靠 `_isolate` 的默认置 0 不受影响）。
本模块**无 FastAPI 依赖**（依赖在 server 段 2 接）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_USERS = REPO_ROOT / "data" / "users.json"
_SECRET_PATH = REPO_ROOT / "data" / "auth_secret"
COOKIE_NAME = "ra_auth"
COOKIE_MAX_AGE = 30 * 86400


@dataclass
class User:
    id: str
    name: str
    admin: bool = False
    limits: dict | None = field(default_factory=lambda: {"standard": 2, "fast": 6})
    disabled: bool = False


# RA_AUTH=0 的固定身份：admin，绕过一切归属/限额（=今天的行为）。
LOCAL_ADMIN = User(id="local", name="local", admin=True, limits=None)


def auth_enabled() -> bool:
    return os.environ.get("RA_AUTH", "1").lower() not in ("0", "false", "off")


def users_path() -> Path:
    return Path(os.environ.get("RA_USERS_PATH", str(DEFAULT_USERS)))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, token_sha256: str) -> bool:
    return hmac.compare_digest(hash_token(token), token_sha256)


_cache: dict = {}


def _load_raw() -> dict:
    """读用户库，按 (mtime_ns, size, ino) 三元组缓存——`os.replace` 每次换
    inode，禁用/轮换即时生效（不靠秒级 mtime 粒度）。"""
    p = users_path()
    try:
        st = p.stat()
    except FileNotFoundError:
        return {"users": {}}
    key = (st.st_mtime_ns, st.st_size, st.st_ino)
    if _cache.get("_key") == key and _cache.get("_path") == str(p):
        return _cache
    raw = json.loads(p.read_text("utf-8"))
    _cache.clear()
    _cache.update({"_key": key, "_path": str(p), "users": raw})
    return _cache


def load_users() -> list[User]:
    out = []
    for uid, u in (_load_raw().get("users") or {}).items():
        out.append(User(id=uid, name=u.get("name", uid),
                        admin=bool(u.get("admin")),
                        limits=u.get("limits"),
                        disabled=bool(u.get("disabled"))))
    return out


def get_user(uid: str) -> User | None:
    for u in load_users():
        if u.id == uid and not u.disabled:
            return u
    return None


def _save_users(users: dict) -> None:
    """原子写：临时文件 + rename；写完 chmod 600。"""
    p = users_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".users.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(users, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    os.chmod(p, 0o600)


def add_user(uid: str, name: str, token: str, admin: bool = False,
             limits: dict | None = ...) -> None:
    users = _load_raw().get("users") or {}
    if uid in users:
        raise ValueError(f"用户 {uid} 已存在")
    if limits is ...:
        limits = None if admin else {"standard": 2, "fast": 6}
    users[uid] = {"name": name, "admin": admin,
                  "token_sha256": hash_token(token),
                  "limits": limits, "disabled": False}
    _save_users(users)


def set_disabled(uid: str, disabled: bool) -> None:
    users = _load_raw().get("users") or {}
    if uid not in users:
        raise ValueError(f"用户 {uid} 不存在")
    users[uid]["disabled"] = disabled
    _save_users(users)


def rotate_token(uid: str, new_token: str) -> None:
    users = _load_raw().get("users") or {}
    if uid not in users:
        raise ValueError(f"用户 {uid} 不存在")
    users[uid]["token_sha256"] = hash_token(new_token)
    _save_users(users)


def token_uid(token: str) -> str | None:
    """邀请 token → uid（遍历 sha256 比对，常量时间）。"""
    h = hash_token(token)
    for uid, u in (_load_raw().get("users") or {}).items():
        if hmac.compare_digest(h, u.get("token_sha256", "")):
            return uid if not u.get("disabled") else None
    return None


def _secret() -> bytes:
    s = os.environ.get("RA_AUTH_SECRET")
    if s:
        return s.encode("utf-8")
    _SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(10):
        try:
            # O_EXCL 原子创建：多进程并发只有一个赢，其余读同一份。
            fd = os.open(_SECRET_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                         0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32))
        except FileExistsError:
            pass
        data = _SECRET_PATH.read_text("utf-8").strip()
        if data:
            return data.encode("utf-8")
        time.sleep(0.02)   # 另一个进程创建中还没写完
    raise RuntimeError("data/auth_secret 是空文件且等待超时——删掉它重启")


def sign_cookie(uid: str) -> str:
    payload = f"{uid}|{int(time.time())}|{secrets.token_urlsafe(16)}"
    sig = hmac.new(_secret(), payload.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def check_cookie(value: str) -> str | None:
    """验 cookie → uid（校验签名+有效期+用户存在且未禁用；每请求回查）。"""
    try:
        uid, ts, rand, sig = value.rsplit("|", 3)
    except ValueError:
        return None
    payload = f"{uid}|{ts}|{rand}"
    want = hmac.new(_secret(), payload.encode("utf-8"),
                    hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, want):
        return None
    if not (-300 < time.time() - int(ts) < COOKIE_MAX_AGE):
        return None
    return uid if get_user(uid) else None
