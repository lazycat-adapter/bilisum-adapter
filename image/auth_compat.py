"""LazyCat browser sessions and verified OIDC-to-gateway account bindings."""

import base64
from contextlib import closing
from functools import lru_cache
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .auth import SESSION_COOKIE_NAME, extract_bearer_token
from .context import access_token_manager
from .history_compat import has_legacy_history


router = APIRouter(prefix="/auth")
LOGIN_PATH = "/api/v1/auth/oidc/login"
OIDC_COOKIE = "bilisum_oidc_session"
STATE_COOKIE = "bilisum_oidc_state"
GATEWAY_COOKIE = "bilisum_gateway_session"
SESSION_SECONDS = 30 * 24 * 3600
SHARED_ACTOR = "access-token:shared"


def _base64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _signed(payload, secret, purpose):
    # The empty purpose exactly reproduces the 1.19.2 OIDC session format.
    encoded = _base64(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    signature = hmac.digest(secret.encode(), (purpose + encoded).encode(), "sha256").hex()
    return f"{encoded}.{signature}"


def _verified(cookie, secret, purpose):
    if not secret or not cookie or not cookie.isascii() or len(cookie) > 8192:
        return None
    encoded, separator, signature = cookie.rpartition(".")
    if not separator:
        return None
    expected = hmac.digest(secret.encode(), (purpose + encoded).encode(), "sha256").hex()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or type(payload.get("expires_at")) is not int:
        return None
    return payload if payload["expires_at"] > time.time() else None


def _identity(value):
    return isinstance(value, str) and 0 < len(value) <= 512 and value.isprintable() and value.strip() == value


def _personal_actor(value):
    if not isinstance(value, str):
        return False
    prefix, _, identifier = value.partition(":")
    return prefix in {"oidc", "inject"} and _identity(identifier)


def _session_secret(manager):
    return (manager._env.get("VIDEO_SUM_SESSION_SECRET") or "").strip()


@lru_cache(maxsize=8)
def _instance_key(path, deployment_secret):
    # The platform's stable secret can outlive an instance. A private salt in
    # the mounted DB also binds new sessions to the installation that issued them.
    with closing(sqlite3.connect(path, timeout=15)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS lzc_auth_instance (
            id INTEGER PRIMARY KEY CHECK (id = 1), salt BLOB NOT NULL
        )""")
        db.execute("INSERT OR IGNORE INTO lzc_auth_instance VALUES (1, randomblob(32))")
        salt = db.execute("SELECT salt FROM lzc_auth_instance WHERE id = 1").fetchone()[0]
    return hmac.digest(deployment_secret.encode(), salt, "sha256").hex()


def _cookie_key(request, manager, *, gateway=False):
    if gateway:
        secret = manager._env.get("BILISUM_INJECT_SECRET") or ""
    else:
        secret = _session_secret(manager)
    if not secret:
        raise HTTPException(503, "未配置会话签名密钥。")
    return _instance_key(_db_path(request), secret)


def _bound_subject(path, subject, uid, *, legacy_owner=False):
    if not path.exists():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        exists = db.execute("SELECT 1 FROM sqlite_schema WHERE name = 'lzc_identity_bindings'").fetchone()
        if exists:
            rows = db.execute(
                "SELECT actor, gateway_uid, oidc_subject FROM lzc_identity_bindings "
                "WHERE gateway_uid = ? OR oidc_subject = ?", (uid, subject),
            ).fetchall()
            for actor, gateway_uid, oidc_subject in rows:
                if gateway_uid == uid and oidc_subject == subject:
                    return actor
                if gateway_uid is not None:
                    return None
    # In the original user's single-user installation, the original signed
    # cookie still owns its exact subject. This read never writes a binding.
    if legacy_owner and has_legacy_history(path):
        return "oidc:" + subject
    return None


def _session(request, manager):
    try:
        uid = _gateway_uid(request, manager)
    except HTTPException:
        # Middleware runs outside the route exception handler.
        return None
    gateway_cookie = request.cookies.get(GATEWAY_COOKIE)
    if gateway_cookie and manager._env.get("BILISUM_INJECT_SECRET"):
        gateway = _verified(gateway_cookie, _cookie_key(request, manager, gateway=True), "gateway:")
        if gateway and _personal_actor(gateway.get("actor")) and _identity(gateway.get("uid")):
            if uid and uid != gateway["uid"]:
                return None
            return gateway["actor"], {"authMethod": "inject", "user": {"name": gateway["uid"]}}

    oidc_cookie = request.cookies.get(OIDC_COOKIE) or ""
    is_legacy = not oidc_cookie.startswith("v2.")
    if not is_legacy and _session_secret(manager):
        oidc = _verified(oidc_cookie[3:], _cookie_key(request, manager), "oidc:")
    else:
        oidc = _verified(oidc_cookie, _session_secret(manager), "")
    if oidc and _identity(oidc.get("sub")):
        subject = oidc["sub"]
        actor = f"oidc:{subject}" if is_legacy else oidc.get("actor")
        legacy_owner = is_legacy and uid and uid == manager._env.get("BILISUM_DEPLOY_UID")
        if uid:
            if not is_legacy and oidc.get("gateway_uid") is not None:
                if uid != oidc["gateway_uid"]:
                    return None
            else:
                # Old signed cookies lack a gateway ID, as do OIDC-only
                # logins. Only the durable two-proof binding can match them.
                actor = _bound_subject(_db_path(request), subject, uid, legacy_owner=legacy_owner)
                if actor is None:
                    return None
        if _personal_actor(actor):
            profile = {key: oidc.get(key, "") for key in ("sub", "name", "email", "role")}
            status = {"authMethod": "oidc", "user": profile}
            if legacy_owner and subject == uid and has_legacy_history(_db_path(request)):
                status["canAutoBind"] = True
            return actor, status

    bearer = extract_bearer_token(request)
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if (bearer and manager.verify(bearer)) or (cookie and manager.verify(cookie)):
        return SHARED_ACTOR, {"authMethod": "access_token"}
    return None


def actor_for_request(request: Request) -> str:
    session = _session(request, access_token_manager)
    if session is None:
        raise HTTPException(401, "请先登录。")
    return session[0]


def authenticated(request, manager):
    return _session(request, manager) is not None


def _oidc_config(manager):
    env = manager._env
    return {
        "client_id": env.get("LAZYCAT_AUTH_OIDC_CLIENT_ID"),
        "client_secret": env.get("LAZYCAT_AUTH_OIDC_CLIENT_SECRET"),
        "auth_uri": env.get("LAZYCAT_AUTH_OIDC_AUTH_URI"),
        "token_uri": env.get("LAZYCAT_AUTH_OIDC_TOKEN_URI"),
        "userinfo_uri": env.get("LAZYCAT_AUTH_OIDC_USERINFO_URI"),
        "redirect_uri": env.get("VIDEO_SUM_OIDC_REDIRECT_URI"),
    }


def auth_status(request, manager):
    session = _session(request, manager)
    enabled = all(_oidc_config(manager).values()) and bool(_session_secret(manager))
    return {
        "authenticated": session is not None,
        **(session[1] if session else {}),
        "oidcEnabled": enabled,
        "oidcLoginPath": LOGIN_PATH if enabled else "",
    }


def _gateway_uid(request, manager):
    grant = request.headers.getlist("x-bilisum-grant")
    uid = request.headers.getlist("x-bilisum-uid")
    if not grant and not uid:
        return None
    expected = manager._env.get("BILISUM_INJECT_SECRET") or ""
    if (not expected or len(grant) != 1 or len(uid) != 1 or not _identity(uid[0])
            or not hmac.compare_digest(grant[0].encode(), expected.encode())):
        raise HTTPException(401, "网关身份无效。")
    return uid[0]


def _db_path(request):
    filename = request.app.state.auth_database_file
    if not filename:
        raise HTTPException(503, "个人登录需要持久化 SQLite 数据库。")
    return Path(filename)


def _resolve_binding(path, uid, subject=None):
    """Serialize first login and linking across concurrent requests/workers."""
    with closing(sqlite3.connect(path, timeout=15)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        db.execute("""
            CREATE TABLE IF NOT EXISTS lzc_identity_bindings (
                actor TEXT PRIMARY KEY NOT NULL,
                gateway_uid TEXT UNIQUE,
                oidc_subject TEXT UNIQUE,
                CHECK (gateway_uid IS NOT NULL OR oidc_subject IS NOT NULL)
            )
        """)
        gateway = db.execute(
            "SELECT * FROM lzc_identity_bindings WHERE gateway_uid = ?", (uid,)
        ).fetchone()
        if subject is None:
            if gateway:
                return gateway["actor"]
            pending_oidc = db.execute(
                "SELECT 1 FROM lzc_identity_bindings WHERE gateway_uid IS NULL LIMIT 1"
            ).fetchone()
            if pending_oidc or has_legacy_history(path):
                raise HTTPException(409, "请先通过懒猫 OIDC 登录一次，关联已有身份。")
            actor = "inject:" + secrets.token_hex(16)
            db.execute("INSERT INTO lzc_identity_bindings (actor, gateway_uid) VALUES (?, ?)", (actor, uid))
            return actor

        oidc = db.execute(
            "SELECT * FROM lzc_identity_bindings WHERE oidc_subject = ?", (subject,)
        ).fetchone()
        if oidc:
            if uid and (oidc["gateway_uid"] not in (None, uid) or (gateway and gateway["actor"] != oidc["actor"])):
                raise HTTPException(409, "身份已关联到其他账号。")
            if uid and oidc["gateway_uid"] is None:
                db.execute("UPDATE lzc_identity_bindings SET gateway_uid = ? WHERE actor = ?", (uid, oidc["actor"]))
            return oidc["actor"]
        if gateway:
            if gateway["oidc_subject"] is not None or has_legacy_history(path):
                raise HTTPException(409, "账号已关联其他身份，无法自动关联。")
            db.execute("UPDATE lzc_identity_bindings SET oidc_subject = ? WHERE actor = ?", (subject, gateway["actor"]))
            return gateway["actor"]

        actor = "oidc:" + subject
        db.execute("INSERT INTO lzc_identity_bindings VALUES (?, ?, ?)", (actor, uid, subject))
        return actor


def _set_cookie(response, key, value, seconds):
    response.set_cookie(key, value, httponly=True, secure=True, samesite="lax", path="/", max_age=seconds)
    response.headers["Cache-Control"] = "no-store"


def clear_personal_cookies(response):
    for cookie in (GATEWAY_COOKIE, OIDC_COOKIE, STATE_COOKIE):
        response.delete_cookie(cookie, path="/", secure=True, httponly=True, samesite="lax")


@router.post("/auto-session")
def auto_session(request: Request):
    if request.headers.get("x-requested-with") != "LazyCat-Login":
        raise HTTPException(403, "请求缺少同源登录标记。")
    fetch_site = request.headers.get("sec-fetch-site")
    origin = request.headers.get("origin")
    redirect_uri = _oidc_config(access_token_manager)["redirect_uri"] or ""
    allowed = {str(request.url.replace(path="", query="")).rstrip("/")}
    if redirect_uri:
        callback = urlsplit(redirect_uri)
        allowed.add(f"{callback.scheme}://{callback.netloc}")
    if (fetch_site and fetch_site != "same-origin") or (origin and origin not in allowed):
        raise HTTPException(403, "仅接受同源登录请求。")
    uid = _gateway_uid(request, access_token_manager)
    if uid is None:
        raise HTTPException(401, "需要经过认证的网关。")
    path = _db_path(request)
    legacy_subject = None
    if uid == access_token_manager._env.get("BILISUM_DEPLOY_UID"):
        old = _verified(request.cookies.get(OIDC_COOKIE), _session_secret(access_token_manager), "")
        if old and _identity(old.get("sub")) and has_legacy_history(path):
            subject = old["sub"]
            if _bound_subject(path, subject, uid, legacy_owner=True):
                if subject != uid:
                    # A user's old session remains usable, but an opaque OIDC
                    # subject requires a new OIDC round trip for durable linking.
                    return JSONResponse({"authenticated": True, "authMethod": "oidc"},
                                        headers={"Cache-Control": "no-store"})
                legacy_subject = subject
    secret = _cookie_key(request, access_token_manager, gateway=True)
    actor = _resolve_binding(path, uid, legacy_subject)
    payload = {"actor": actor, "uid": uid, "expires_at": int(time.time()) + SESSION_SECONDS}
    response = JSONResponse({"authenticated": True, "authMethod": "inject"})
    response.delete_cookie(OIDC_COOKIE, path="/")
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    _set_cookie(response, GATEWAY_COOKIE, _signed(payload, secret, "gateway:"), SESSION_SECONDS)
    return response


def _require_oidc():
    config = _oidc_config(access_token_manager)
    if not all(config.values()):
        raise HTTPException(404, "当前部署未启用懒猫 OIDC。")
    return config


@router.get("/oidc/login", include_in_schema=False)
def start_oidc(request: Request):
    config = _require_oidc()
    secret = _cookie_key(request, access_token_manager)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    payload = {"state": state, "uid": _gateway_uid(request, access_token_manager), "verifier": verifier, "expires_at": int(time.time()) + 600}
    query = urlencode({
        "client_id": config["client_id"], "redirect_uri": config["redirect_uri"],
        "response_type": "code", "scope": "openid profile email groups", "state": state,
        "code_challenge": _base64(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    })
    separator = "&" if "?" in config["auth_uri"] else "?"
    response = RedirectResponse(config["auth_uri"] + separator + query, status_code=302)
    _set_cookie(response, STATE_COOKIE, _signed(payload, secret, "state:"), 600)
    return response


def _fetch_profile(config, code, verifier):
    try:
        tokens = httpx.post(config["token_uri"], data={
            "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
            "client_id": config["client_id"], "client_secret": config["client_secret"],
            "redirect_uri": config["redirect_uri"],
        }, timeout=20)
        tokens.raise_for_status()
        token = tokens.json()["access_token"]
        if not isinstance(token, str) or not token:
            raise ValueError("missing access token")
        result = httpx.get(config["userinfo_uri"], headers={"Authorization": f"Bearer {token}"}, timeout=20)
        result.raise_for_status()
        profile = result.json()
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(502, "无法从懒猫 OIDC 服务获取用户信息。") from exc
    if not isinstance(profile, dict) or not _identity(profile.get("sub") or profile.get("uid")):
        raise HTTPException(502, "OIDC userinfo 缺少用户标识。")
    return profile


@router.get("/oidc/callback", include_in_schema=False)
def finish_oidc(request: Request, state: str = "", code: str = "", error: str = ""):
    config = _require_oidc()
    secret = _cookie_key(request, access_token_manager)
    proof = _verified(request.cookies.get(STATE_COOKIE), secret, "state:")
    if (error or not code or not proof or proof.get("state") != state
            or proof.get("uid") != _gateway_uid(request, access_token_manager)):
        raise HTTPException(400, "OIDC 登录校验失败，请重新登录。")
    profile = _fetch_profile(config, code, proof["verifier"])
    subject = profile.get("sub") or profile["uid"]
    actor = _resolve_binding(_db_path(request), proof["uid"], subject)
    groups = profile.get("groups")
    issued = int(time.time())
    payload = {
        "sub": subject, "actor": actor, "gateway_uid": proof["uid"],
        "name": str(profile.get("name") or profile.get("preferred_username") or subject)[:256],
        "email": str(profile.get("email") or "")[:256],
        "role": "ADMIN" if isinstance(groups, list) and "ADMIN" in groups else "NORMAL",
        "issued_at": issued, "expires_at": issued + SESSION_SECONDS,
    }
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(STATE_COOKIE, path="/")
    response.delete_cookie(GATEWAY_COOKIE, path="/")
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    _set_cookie(response, OIDC_COOKIE, "v2." + _signed(payload, secret, "oidc:"), SESSION_SECONDS)
    return response


@router.post("/logout")
def logout():
    response = JSONResponse({"authenticated": False}, headers={"Cache-Control": "no-store"})
    clear_personal_cookies(response)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
