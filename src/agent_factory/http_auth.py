"""Local HTTP authority and short-lived, process-local browser sessions.

This is a single-operator boundary, not a multi-user identity provider. Domain
services retain their existing approval, role and execution-fence checks.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from urllib.parse import urlsplit

COOKIE = 'agent_factory_session'
SCOPES = frozenset({'read', 'write', 'approve', 'control'})
ROLES = frozenset({'mission_owner', 'operations_owner', 'security_reviewer'})
LOOPBACK = frozenset({'localhost', '127.0.0.1', '::1'})


@dataclass(frozen=True)
class Principal:
    actor: str
    role: str
    scopes: frozenset[str]
    tenants: frozenset[str]


@dataclass(frozen=True)
class Policy:
    token: str
    principal: Principal
    ttl: int

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps([
            self.token, self.principal.actor, self.principal.role,
            sorted(self.principal.scopes), sorted(self.principal.tenants), self.ttl,
        ]).encode()).hexdigest()

    @classmethod
    def environment(cls) -> 'Policy':
        actor = os.getenv('AGENT_FACTORY_API_ACTOR', 'Founder').strip()
        role = os.getenv('AGENT_FACTORY_API_ROLE', 'operations_owner').strip()
        scopes = frozenset(x.strip() for x in os.getenv('AGENT_FACTORY_API_SCOPES', 'read,write,approve,control').split(',') if x.strip())
        tenants = frozenset(x.strip() for x in os.getenv('AGENT_FACTORY_API_TENANTS', '*').split(',') if x.strip())
        ttl = int(os.getenv('AGENT_FACTORY_SESSION_TTL_SECONDS', '900'))
        if not actor or role not in ROLES or not scopes or not scopes <= SCOPES or not tenants or not 1 <= ttl <= 3600:
            raise ValueError('Invalid local HTTP authority configuration')
        return cls(os.getenv('AGENT_FACTORY_API_TOKEN', '').strip(), Principal(actor, role, scopes, tenants), ttl)


def trusted_origin(scheme: str, host: str, origin: str | None) -> bool:
    """Require a loopback Host; an Origin, when supplied, must match exactly."""
    try:
        target = urlsplit(f'{scheme}://{host}')
        if (scheme not in {'http', 'https'} or target.hostname not in LOOPBACK
                or target.username is not None or target.password is not None
                or target.netloc != host or target.path or target.query or target.fragment or target.port == 0):
            return False
        if origin is None:
            return True  # CLI clients have no browser Origin.
        source = urlsplit(origin)
        default = 443 if scheme == 'https' else 80
        return (source.scheme == scheme and source.hostname == target.hostname and source.port != 0
                and origin == f'{source.scheme}://{source.netloc}'
                and (source.port or default) == (target.port or default)
                and source.username is None and source.password is None
                and not source.path and not source.query and not source.fragment)
    except (ValueError, TypeError):
        return False


def required_scope(path: str, method: str) -> str:
    if method in {'GET', 'HEAD', 'OPTIONS'}:
        return 'read'
    if path == '/api/control/actions':
        return 'control'
    if (path.startswith('/api/approvals/') or path.startswith('/api/founder-decisions/')
            or (path.startswith('/api/artifacts/') and path.endswith('/review'))):
        return 'approve'
    return 'write'


class LocalAccess:
    def __init__(self, *, clock=time.monotonic):
        self.required = bool(Policy.environment().token)
        self.clock = clock
        self.sessions: dict[str, tuple[float, str]] = {}
        self.lock = threading.Lock()

    def policy(self) -> Policy:
        policy = Policy.environment()
        # Removing a configured credential must not silently open a running app.
        if self.required and not policy.token:
            raise ValueError('Configured local HTTP credential is unavailable')
        if policy.token:
            self.required = True
        return policy

    @staticmethod
    def matches(token: str, candidate: str) -> bool:
        return bool(token) and hmac.compare_digest(token.encode(), candidate.encode())

    @staticmethod
    def key(cookie: str) -> str:
        return hashlib.sha256(cookie.encode()).hexdigest()

    def authenticate(self, policy: Policy, authorization: str | None, cookie: str | None) -> Principal | None:
        if not policy.token:
            return policy.principal  # Explicitly documented local-open mode.
        if authorization is not None:
            # A bad explicit bearer never falls back to an ambient session.
            return policy.principal if self.matches(policy.token, authorization.removeprefix('Bearer ')) and authorization.startswith('Bearer ') else None
        if not cookie:
            return None
        key = self.key(cookie)
        with self.lock:
            record = self.sessions.get(key)
            if record is None:
                return None
            if record[0] <= self.clock() or record[1] != policy.digest:
                self.sessions.pop(key, None)
                return None
        return policy.principal

    def login(self, policy: Policy, candidate: str) -> str | None:
        if not self.matches(policy.token, candidate):
            return None
        cookie = secrets.token_urlsafe(32)
        with self.lock:
            current = self.clock()
            self.sessions = {k: v for k, v in self.sessions.items() if v[0] > current and v[1] == policy.digest}
            if len(self.sessions) >= 128:
                del self.sessions[min(self.sessions, key=lambda k: self.sessions[k][0])]
            self.sessions[self.key(cookie)] = (current + policy.ttl, policy.digest)
        return cookie

    def logout(self, cookie: str | None) -> None:
        if cookie:
            with self.lock:
                self.sessions.pop(self.key(cookie), None)


class LocalHTTPBoundary:
    """Authorize before routing without changing response or dependency lifetime."""

    def __init__(self, app, *, access: LocalAccess):
        self.app = app
        self.access = access

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from starlette.datastructures import MutableHeaders
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        request = Request(scope)

        async def protected_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["Referrer-Policy"] = "no-referrer"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
            await send(message)

        async def deny(status, code):
            response = JSONResponse({"error": {"code": code}}, status_code=status)
            await response(scope, receive, protected_send)

        if (len(request.headers.getlist("host")) != 1
                or len(request.headers.getlist("origin")) > 1
                or len(request.headers.getlist("authorization")) > 1
                or not trusted_origin(scope.get("scheme", "http"),
                                      request.headers.get("host", ""), request.headers.get("origin"))):
            await deny(403, "local_origin_required")
            return
        try:
            policy = self.access.policy()
        except ValueError:
            await deny(503, "local_access_unavailable")
            return
        from starlette.concurrency import run_in_threadpool
        principal = await run_in_threadpool(self.access.authenticate, policy,
                                           request.headers.get("authorization"), request.cookies.get(COOKIE))
        request.state.local_policy = policy
        request.state.local_principal = principal
        path = scope["path"]
        # Self-registration grants a profile and downloads, not access to the
        # operator's shared workspace. Enforce this before every route, including
        # legacy endpoints which predate per-tenant authorization.
        if principal is not None and principal.role == 'account_user':
            allowed = (path in {'/api/account', '/profile', '/organizations', '/login', '/register',
                                '/downloads', '/downloads/manifest.json', '/auth/session'}
                       or path.startswith('/auth/account') or path.startswith('/auth/sso/')
                       or path.startswith('/downloads/files/') or path.startswith('/assets/')
                       or path == '/authorize' or path.startswith('/backchannel/'))
            if path == '/':
                from starlette.responses import RedirectResponse
                await RedirectResponse('/downloads', status_code=303)(scope, receive, protected_send)
                return
            if not allowed:
                await deny(403, 'personal_account_only')
                return
        if path == "/api" or path.startswith("/api/"):
            if principal is None:
                await deny(401, "authentication_required")
                return
            if required_scope(path, scope["method"]) not in principal.scopes:
                await deny(403, "scope_required")
                return
        # Direct ASGI forwarding adds neither a streaming response nor background
        # task boundaries around the endpoint's yield dependencies.
        await self.app(scope, receive, protected_send)
