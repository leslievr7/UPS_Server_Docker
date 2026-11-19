"""Sign-in and form protection for the web GUI.

One admin password protects every page. What it covers:

- The password is stored only as a slow, salted hash (werkzeug, already part of
  Flask), in a small JSON file next to power_manager.conf so it survives
  container rebuilds: /etc/nut/web_auth.json.
- The file also holds a random session secret, created on first start. It
  replaces the old hard-coded secret, so nobody can forge a signed-in cookie.
- Every form and every fetch() that changes something must carry a CSRF token
  from the user's own session. A page on another site cannot read that token,
  so it cannot trick a signed-in browser into saving settings or waking a machine.
- After repeated wrong passwords from one address, sign-in is refused for a
  while instead of slowing the server down.

The API that the UPS clients use (port 5000) is a separate app with its own
token and is not affected.

gunicorn runs the GUI in more than one worker process, so everything shared
lives in the file: each worker notices when it changes (password set or changed)
and reloads it.
"""

import hmac
import json
import os
import secrets
import threading
import time
from datetime import timedelta

from flask import (flash, jsonify, redirect, render_template, request,
                   session, url_for)
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import check_password_hash, generate_password_hash

AUTH_FILE = "/etc/nut/web_auth.json"

MIN_PASSWORD_LENGTH = 8
# pbkdf2 instead of werkzeug's default scrypt: scrypt needs ~32 MB of RAM per
# check, which is a lot on a 512 MB board. 200k rounds takes about a second there.
HASH_METHOD = 'pbkdf2:sha256:200000'
SESSION_DAYS = 30

MAX_FAILURES = 5          # wrong passwords allowed before a pause
LOCKOUT_SECONDS = 60      # first pause; doubles with each further failure
MAX_LOCKOUT_SECONDS = 15 * 60

# Endpoints anyone may open. Everything else needs a signed-in session.
PUBLIC_ENDPOINTS = {'login', 'setup', 'static', 'version_endpoint'}
# Extra public endpoint prefixes, e.g. the sandbox's own control panel.
PUBLIC_PREFIXES = []
# Where to go after creating the password. Set by web_onboarding.
after_setup = None

_lock = threading.Lock()
_cache = {'mtime': None, 'data': {}}
_failures = {}  # remote address -> (count, locked_until)


# --- the file -----------------------------------------------------------------

def _write(data):
    """Replace the file in one step, readable only by its owner."""
    folder = os.path.dirname(AUTH_FILE) or '.'
    tmp = os.path.join(folder, f'.web_auth.{os.getpid()}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, AUTH_FILE)


def _load():
    """The current file contents, re-read only when the file has changed."""
    with _lock:
        try:
            mtime = os.stat(AUTH_FILE).st_mtime_ns
        except FileNotFoundError:
            mtime = None
        if mtime is None:
            # First start: create the secret. O_EXCL means only one worker
            # wins; the others read what it wrote.
            data = {'secret_key': secrets.token_hex(32)}
            try:
                fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as f:
                    json.dump(data, f, indent=2)
            except FileExistsError:
                pass
            mtime = os.stat(AUTH_FILE).st_mtime_ns
        if mtime != _cache['mtime']:
            try:
                with open(AUTH_FILE) as f:
                    _cache['data'] = json.load(f)
            except (OSError, ValueError):
                # Half-written by another worker: keep what we had, retry next time.
                return _cache['data']
            _cache['mtime'] = mtime
        return _cache['data']


def get_flag(name):
    """A small remembered fact about this install (e.g. onboarding finished)."""
    return bool(_load().get('flags', {}).get(name))


def set_flag(name, value=True):
    data = dict(_load())
    flags = dict(data.get('flags', {}))
    if bool(flags.get(name)) == bool(value):
        return
    flags[name] = bool(value)
    data['flags'] = flags
    _write(data)
    _cache['mtime'] = None


def has_password():
    return bool(_load().get('password_hash'))


def set_password(password):
    """Store a new password and a new session secret.

    A new secret signs everyone out, including other devices - which is what
    you want after changing a password.
    """
    data = dict(_load())
    data['password_hash'] = generate_password_hash(password, method=HASH_METHOD)
    data['secret_key'] = secrets.token_hex(32)
    data['changed_at'] = int(time.time())
    _write(data)
    _cache['mtime'] = None


def check_password(password):
    stored = _load().get('password_hash')
    return bool(stored) and check_password_hash(stored, password)


def password_problem(password, confirm):
    """A plain-words reason the new password can't be used, or None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f'Use at least {MIN_PASSWORD_LENGTH} characters.'
    if password != confirm:
        return "The two passwords don't match."
    return None


# --- wrong-password pauses --------------------------------------------------

def _client():
    return request.remote_addr or 'unknown'


def seconds_locked():
    _, until = _failures.get(_client(), (0, 0))
    return max(0, int(until - time.time()))


def record_failure():
    count, _ = _failures.get(_client(), (0, 0))
    count += 1
    until = 0
    if count >= MAX_FAILURES:
        until = time.time() + min(LOCKOUT_SECONDS * 2 ** (count - MAX_FAILURES), MAX_LOCKOUT_SECONDS)
    _failures[_client()] = (count, until)


def clear_failures():
    _failures.pop(_client(), None)


# --- CSRF ---------------------------------------------------------------------

def csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_urlsafe(32)
    return session['_csrf']


def _csrf_ok():
    expected = session.get('_csrf')
    sent = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token')
    return bool(expected and sent and hmac.compare_digest(expected, sent))


# --- wiring -------------------------------------------------------------------

def _is_public(endpoint):
    endpoint = endpoint or ''
    return endpoint in PUBLIC_ENDPOINTS or any(endpoint.startswith(p) for p in PUBLIC_PREFIXES)


def _wants_json():
    return request.headers.get('X-Requested-With') == 'fetch' or request.is_json


class _FileSecretSessions(SecureCookieSessionInterface):
    """Signs session cookies with the secret from AUTH_FILE.

    Flask opens the session before any before_request hook runs, so the secret
    has to be supplied here. Reading it each time (cached until the file
    changes) also means every worker switches to a new secret as soon as the
    password is changed.
    """

    def get_signing_serializer(self, app):
        app.secret_key = _load()['secret_key']
        return super().get_signing_serializer(app)


def init_app(app):
    """Protect every route of `app`."""
    app.session_interface = _FileSecretSessions()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS),
    )

    @app.before_request
    def _guard():
        endpoint = request.endpoint
        changes_something = request.method not in ('GET', 'HEAD', 'OPTIONS')
        if changes_something and (not _is_public(endpoint) or endpoint in ('login', 'setup')):
            if not _csrf_ok():
                if _wants_json():
                    return jsonify({'success': False, 'message': 'This page has expired. Reload it and try again.'}), 400
                flash('That form had expired, so nothing was changed. Please try again.', 'error')
                return redirect(request.referrer or url_for('index'))

        if _is_public(endpoint):
            return None
        if not has_password():
            return redirect(url_for('setup'))
        if not session.get('signed_in'):
            if _wants_json():
                return jsonify({'success': False, 'message': 'Signed out', 'signed_out': True}), 401
            target = request.full_path.rstrip('?') if request.method == 'GET' else None
            return redirect(url_for('login', next=None if target == '/' else target))
        return None

    @app.context_processor
    def _inject():
        return {'csrf_token': csrf_token, 'signed_in': bool(session.get('signed_in'))}

    def _safe_next(target):
        # Only ever send people back to a page on this site.
        if target and target.startswith('/') and not target.startswith('//'):
            return target.rstrip('?')
        return url_for('index')

    @app.route('/setup', methods=['GET', 'POST'])
    def setup():
        if has_password():
            return redirect(url_for('login'))
        error = None
        if request.method == 'POST':
            password = request.form.get('password', '')
            error = password_problem(password, request.form.get('confirm', ''))
            if not error:
                set_password(password)
                session.clear()
                session.permanent = True
                session['signed_in'] = True
                if after_setup:
                    return redirect(after_setup())
                flash('Password created. You are signed in on this device.', 'success')
                return redirect(url_for('index'))
        return render_template('setup.html', error=error, min_length=MIN_PASSWORD_LENGTH)

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if not has_password():
            return redirect(url_for('setup'))
        if session.get('signed_in'):
            return redirect(_safe_next(request.args.get('next')))
        error = None
        if request.method == 'POST':
            wait = seconds_locked()
            if wait:
                error = f'Too many wrong passwords. Try again in {wait} seconds.'
            elif check_password(request.form.get('password', '')):
                clear_failures()
                session.clear()
                session.permanent = True
                session['signed_in'] = True
                return redirect(_safe_next(request.form.get('next')))
            else:
                record_failure()
                wait = seconds_locked()
                error = (f'Too many wrong passwords. Try again in {wait} seconds.' if wait
                         else "That password isn't right.")
        return render_template('login.html', error=error, next=request.values.get('next', ''))

    @app.route('/logout', methods=['POST'])
    def logout():
        session.clear()
        flash('You are signed out.', 'success')
        return redirect(url_for('login'))

    @app.route('/security/password', methods=['POST'])
    def change_password():
        if not check_password(request.form.get('current', '')):
            record_failure()
            flash("Your current password isn't right, so nothing was changed.", 'error')
            return redirect(url_for('config') + '#security')
        clear_failures()
        password = request.form.get('password', '')
        problem = password_problem(password, request.form.get('confirm', ''))
        if problem:
            flash(problem + ' Nothing was changed.', 'error')
            return redirect(url_for('config') + '#security')
        set_password(password)
        session.clear()
        session.permanent = True
        session['signed_in'] = True
        flash('Password changed. Other devices have been signed out.', 'success')
        return redirect(url_for('config') + '#security')
