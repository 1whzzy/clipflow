"""Feishu QR login and browser sessions for the shared workbench."""
import hashlib
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

CALLBACK = "/api/v1/auth/feishu/callback"
STATE_COOKIE = "foundation_login_state"
SESSION_COOKIE = "foundation_employee"
STATE_AGE = 600
SESSION_AGE = 8 * 3600
FEISHU_API = "https://open.feishu.cn/open-apis"
FEISHU_AUTHORIZE = "https://passport.feishu.cn/suite/passport/oauth/authorize"
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
AUTH_PATHS = {"/api/v1/auth/session", "/api/v1/auth/feishu/start", CALLBACK, "/api/v1/auth/logout"}
API_DOCS = {"/api", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}


@dataclass(frozen=True)
class FeishuSettings:
    app_id: str
    app_secret: str = field(repr=False)
    tenant_key: str = field(repr=False)
    redirect_uri: str

    def __post_init__(self):
        if not all((self.app_id, self.app_secret, self.tenant_key, self.redirect_uri)):
            raise ValueError("Feishu login needs its app ID, app secret, tenant key and callback URL")
        url = urlsplit(self.redirect_uri)
        loopback = url.hostname in ("127.0.0.1", "localhost", "::1")
        if (not url.hostname or url.username is not None or url.path != CALLBACK
                or url.query or url.fragment or not (url.scheme == "https" or (url.scheme == "http" and loopback))):
            raise ValueError(f"Feishu callback must be an HTTPS URL ending in {CALLBACK} (HTTP loopback for testing)")

    @classmethod
    def from_env(cls, env: dict[str, str]):
        values = [env.get(f"FOUNDATION_FEISHU_{key}", "") for key in
                  ("APP_ID", "APP_SECRET", "TENANT_KEY", "REDIRECT_URI")]
        return cls(*values) if any(values) else None

    @property
    def origin(self) -> str:
        url = urlsplit(self.redirect_uri)
        return f"{url.scheme}://{url.netloc}"

    @property
    def secure(self) -> bool:
        return self.origin.startswith("https:")

    def authorize_url(self, state: str) -> str:
        return FEISHU_AUTHORIZE + "?" + urlencode({
            "client_id": self.app_id, "redirect_uri": self.redirect_uri, "response_type": "code", "state": state,
        })


class LoginFailed(RuntimeError):
    pass


class WrongTenant(LoginFailed):
    pass


def feishu_identity(settings: FeishuSettings, code: str) -> dict[str, str]:
    """Exchange the one-time code, then get only the identity needed by the workbench."""
    try:
        with httpx.Client(timeout=8.0, trust_env=False) as http:
            token = http.post(f"{FEISHU_API}/authen/v2/oauth/token", json={
                "grant_type": "authorization_code", "client_id": settings.app_id,
                "client_secret": settings.app_secret, "code": code, "redirect_uri": settings.redirect_uri,
            }).raise_for_status().json()
            access = token["access_token"]
            if token.get("code", 0) != 0 or not isinstance(access, str) or not access:
                raise LoginFailed
            info = http.get(f"{FEISHU_API}/authen/v1/user_info", headers={
                "Authorization": f"Bearer {access}",
            }).raise_for_status().json()
        if info["code"] != 0:
            raise LoginFailed
        user = info["data"]
        if user["tenant_key"] != settings.tenant_key:
            raise WrongTenant
        if not isinstance(user["open_id"], str) or not user["open_id"].strip():
            raise LoginFailed
        name = user["name"].strip() if isinstance(user["name"], str) else ""
        if not name:
            raise LoginFailed
        return {"open_id": user["open_id"], "name": name[:128]}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        raise LoginFailed from None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def scoped_request_id(request: Request, candidate: str | None) -> str | None:
    employee = getattr(request.state, "employee", None)
    if employee and candidate:
        return "feishu:" + _digest(f"{employee['open_id']}\0{candidate}")
    return candidate


def actor(request: Request, fallback: str = "") -> str:
    employee = getattr(request.state, "employee", None)
    return employee["name"] if employee else fallback


def mount_login(api: FastAPI, pool, settings: FeishuSettings | None) -> None:
    @api.get("/api/v1/auth/session")
    def session(request: Request):
        if settings is None:
            return JSONResponse({"enabled": False, "user": None}, headers=NO_STORE)
        return JSONResponse({"enabled": True, "user": request.state.employee}, headers=NO_STORE)

    if settings is None:
        return

    cookies = {"httponly": True, "samesite": "lax", "secure": settings.secure}

    def find_employee(token: str):
        with pool.connection() as conn:
            return conn.execute(
                """SELECT open_id, name FROM employee_sessions
                   WHERE digest = %s AND app_id = %s AND tenant_key = %s AND expires_at > now()""",
                (_digest(token), settings.app_id, settings.tenant_key),
            ).fetchone()

    @api.middleware("http")
    async def employee_gate(request: Request, call_next):
        path = request.url.path.rstrip("/")
        if not (path.startswith("/api/") or path in API_DOCS):
            return await call_next(request)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and (
            request.headers.get("sec-fetch-site") == "cross-site"
            or request.headers.get("origin", settings.origin) != settings.origin
        ):
            return JSONResponse({"detail": "请求来源不匹配"}, status_code=403, headers=NO_STORE)
        if path not in AUTH_PATHS or path == "/api/v1/auth/session":
            token = request.cookies.get(SESSION_COOKIE)
            request.state.employee = await run_in_threadpool(find_employee, token) if token else None
            if request.state.employee is None and path not in AUTH_PATHS:
                return JSONResponse({"detail": "请先使用飞书扫码登录"}, status_code=401, headers=NO_STORE)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @api.post("/api/v1/auth/feishu/start")
    def start(request: Request):
        state = secrets.token_urlsafe(32)
        with pool.connection() as conn:
            conn.execute("DELETE FROM login_attempts WHERE expires_at <= now() OR digest = %s",
                         (_digest(request.cookies.get(STATE_COOKIE, "")),))
            conn.execute("DELETE FROM employee_sessions WHERE expires_at <= now()")
            conn.execute("INSERT INTO login_attempts VALUES (%s, now() + %s * interval '1 second')",
                         (_digest(state), STATE_AGE))
        response = JSONResponse({"authorize_url": settings.authorize_url(state)}, headers=NO_STORE)
        response.set_cookie(STATE_COOKIE, state, max_age=STATE_AGE, **cookies)
        return response

    @api.get(CALLBACK)
    def callback(request: Request, code: str = "", state: str = ""):
        response = RedirectResponse("/?login=expired", status_code=303, headers=NO_STORE)
        state_cookie = request.cookies.get(STATE_COOKIE, "")
        if not state_cookie or not secrets.compare_digest(state.encode(), state_cookie.encode()):
            return response
        with pool.connection() as conn:
            claimed = conn.execute(
                "DELETE FROM login_attempts WHERE digest = %s AND expires_at > now() RETURNING digest",
                (_digest(state),),
            ).fetchone()
        if not claimed:
            return response
        response.delete_cookie(STATE_COOKIE, **cookies)
        if not code:
            response.headers["Location"] = "/?login=cancelled"
            return response
        try:
            employee = feishu_identity(settings, code)
        except WrongTenant:
            response.headers["Location"] = "/?login=tenant"
            return response
        except LoginFailed:
            response.headers["Location"] = "/?login=failed"
            return response
        token = secrets.token_urlsafe(32)
        with pool.connection() as conn:
            conn.execute("DELETE FROM employee_sessions WHERE digest = %s",
                         (_digest(request.cookies.get(SESSION_COOKIE, "")),))
            conn.execute(
                """INSERT INTO employee_sessions (digest, app_id, tenant_key, open_id, name, expires_at)
                   VALUES (%s, %s, %s, %s, %s, now() + %s * interval '1 second')""",
                (_digest(token), settings.app_id, settings.tenant_key,
                 employee["open_id"], employee["name"], SESSION_AGE),
            )
        response.headers["Location"] = "/"
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_AGE, **cookies)
        return response

    @api.post("/api/v1/auth/logout")
    def logout(request: Request):
        with pool.connection() as conn:
            conn.execute("DELETE FROM employee_sessions WHERE digest = %s",
                         (_digest(request.cookies.get(SESSION_COOKIE, "")),))
            conn.execute("DELETE FROM login_attempts WHERE digest = %s",
                         (_digest(request.cookies.get(STATE_COOKIE, "")),))
        response = JSONResponse({"ok": True}, headers=NO_STORE)
        response.delete_cookie(SESSION_COOKIE, **cookies)
        response.delete_cookie(STATE_COOKIE, **cookies)
        return response
