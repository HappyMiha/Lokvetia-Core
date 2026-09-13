"""Invited operator identities and revocable sessions, independent of releases."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid
from urllib.parse import urlencode

from .http_auth import LocalAccess, Policy, Principal


def email_address(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('Invalid email address')
    value = value.strip().casefold()
    if len(value) > 254 or not re.fullmatch(r'[^\s@\x00-\x1f]+@[^\s@.]+(?:\.[^\s@.]+)+', value):
        raise ValueError('Invalid email address')
    return value


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600_000).hex()


class Accounts:
    def __init__(self, path: Path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS accounts (
                    email TEXT PRIMARY KEY, salt TEXT NOT NULL, password TEXT NOT NULL,
                    principal TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS invitations (
                    hash TEXT PRIMARY KEY, email TEXT NOT NULL, principal TEXT NOT NULL,
                    expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS email_sessions (
                    hash TEXT PRIMARY KEY, email TEXT NOT NULL, policy TEXT NOT NULL,
                    expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS login_limits (
                    key TEXT PRIMARY KEY, until REAL NOT NULL, attempts INTEGER NOT NULL);
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def invite(self, email: str, principal: Principal, *, ttl=86400) -> str:
        email = email_address(email)
        token = secrets.token_urlsafe(32)
        payload = json.dumps(dict(actor=principal.actor, role=principal.role,
                                  scopes=sorted(principal.scopes), tenants=sorted(principal.tenants)))
        with closing(self.connect()) as db, db:
            if db.execute('SELECT 1 FROM accounts WHERE email=?', (email,)).fetchone():
                raise ValueError('An account already exists for this email')
            db.execute('UPDATE invitations SET used=1 WHERE email=?', (email,))
            db.execute('INSERT INTO invitations VALUES (?,?,?,?,0)',
                       (digest(token), email, payload, self.clock() + ttl))
        return token

    def register(self, email: str, password: str, invitation: str):
        email = email_address(email)
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise ValueError('Use a password of 12 to 128 characters')
        if not isinstance(invitation, str) or len(invitation) > 128:
            raise ValueError('Invitation is invalid or expired')
        # Check before the expensive hash and recheck under the write transaction.
        with closing(self.connect()) as db:
            record = db.execute('SELECT * FROM invitations WHERE hash=?', (digest(invitation),)).fetchone()
        if not record or record['used'] or record['email'] != email or record['expires'] <= self.clock():
            raise ValueError('Invitation is invalid or expired')
        salt = secrets.token_hex(16)
        hashed = password_hash(password, salt)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            updated = db.execute('UPDATE invitations SET used=1 WHERE hash=? AND email=? AND used=0 AND expires>?',
                                 (digest(invitation), email, self.clock()))
            if updated.rowcount != 1:
                raise ValueError('Invitation is invalid or expired')
            try:
                db.execute('INSERT INTO accounts(email,salt,password,principal) VALUES (?,?,?,?)',
                           (email, salt, hashed, record['principal']))
            except sqlite3.IntegrityError as error:
                raise ValueError('Invitation is invalid or expired') from error

    def register_personal(self, email: str, password: str, *, peer: str):
        """Create a personal download account, never an organization operator.

        Email is a login identifier, not a verified identity. Invitations reserve
        their address; signup cannot claim an existing member's authority.
        """
        email = email_address(email)
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise ValueError('Use a password of 12 to 128 characters')
        self.allow_attempt('signup:' + email, 'signup:' + peer)
        actor = 'user-' + uuid.uuid4().hex
        principal = json.dumps(dict(actor=actor, role='account_user',
                                    scopes=['read', 'write'], tenants=['account:' + actor]))
        salt = secrets.token_hex(16)
        hashed = password_hash(password, salt)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM invitations WHERE email=? AND used=0 AND expires>?',
                          (email, self.clock())).fetchone():
                raise ValueError('Use your invitation or another email address')
            try:
                db.execute('INSERT INTO accounts(email,salt,password,principal) VALUES (?,?,?,?)',
                           (email, salt, hashed, principal))
            except sqlite3.IntegrityError as error:
                raise ValueError('This address is unavailable; sign in or use another address') from error

    def allow_attempt(self, email: str, peer: str):
        now = self.clock()
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM login_limits WHERE until <= ?', (now,))
            for key, limit in [(digest('email:' + email), 10), (digest('peer:' + peer), 60)]:
                row = db.execute('SELECT attempts FROM login_limits WHERE key=?', (key,)).fetchone()
                if row and row['attempts'] >= limit:
                    raise ValueError('Too many attempts; try again in 15 minutes')
                db.execute('INSERT INTO login_limits VALUES (?,?,1) ON CONFLICT(key) DO UPDATE SET attempts=attempts+1',
                           (key, now + 900))

    def login(self, email: str, password: str, policy: Policy, *, remember=False, peer='local'):
        email = email_address(email)
        if not isinstance(password, str) or len(password) > 128:
            raise ValueError('Email or password was not accepted')
        self.allow_attempt(email, peer)
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM accounts WHERE email=?', (email,)).fetchone()
        candidate = password_hash(password, row['salt'] if row else '00' * 16)
        if not row or not row['enabled'] or not hmac.compare_digest(candidate, row['password']):
            raise ValueError('Email or password was not accepted')
        token = 'email.' + secrets.token_urlsafe(32)
        ttl = 30 * 86400 if remember else 12 * 3600
        with closing(self.connect()) as db, db:
            db.execute('DELETE FROM email_sessions WHERE expires<=?', (self.clock(),))
            db.execute('INSERT INTO email_sessions VALUES (?,?,?,?)',
                       (digest(token), email, policy.digest, self.clock() + ttl))
            db.execute('DELETE FROM login_limits WHERE key=?', (digest('email:' + email),))
        return token, ttl

    def authenticate(self, token: str, policy: Policy):
        with closing(self.connect()) as db:
            row = db.execute('''SELECT a.principal FROM email_sessions s JOIN accounts a ON a.email=s.email
                WHERE s.hash=? AND s.policy=? AND s.expires>? AND a.enabled=1''',
                             (digest(token), policy.digest, self.clock())).fetchone()
        if not row:
            return None
        data = json.loads(row['principal'])
        scopes = frozenset(data['scopes']) & policy.principal.scopes
        tenants = frozenset(data['tenants'])
        if '*' not in policy.principal.tenants:
            tenants = policy.principal.tenants if '*' in tenants else tenants & policy.principal.tenants
        if not scopes or not tenants:
            return None
        return Principal(data['actor'], data['role'], scopes, tenants)

    def logout(self, token: str):
        with closing(self.connect()) as db, db:
            db.execute('DELETE FROM email_sessions WHERE hash=?', (digest(token),))


class EmailAccess(LocalAccess):
    def __init__(self, path: Path):
        super().__init__()
        self.accounts = Accounts(path)

    def authenticate(self, policy, authorization, cookie):
        # Explicit invalid credentials must never fall back to a browser cookie.
        if authorization is None and cookie and cookie.startswith('email.'):
            return self.accounts.authenticate(cookie, policy)
        return super().authenticate(policy, authorization, cookie)

    def logout(self, cookie):
        if cookie:
            self.accounts.logout(cookie)
        super().logout(cookie)


def main():
    parser = argparse.ArgumentParser(description='Create a private, email-bound operator invitation.')
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--email', required=True)
    parser.add_argument('--origin', required=True)
    args = parser.parse_args()
    if args.origin != 'https://id.lokvetia.com':
        parser.error('Use the configured test HTTPS origin')
    email = email_address(args.email)
    token = Accounts(args.database).invite(email, Policy.environment().principal)
    print(args.origin + '/register#' + urlencode({'invite': token, 'email': email}))


if __name__ == '__main__':
    main()
