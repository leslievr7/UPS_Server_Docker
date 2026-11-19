"""Reverse proxies, log retention, the example config, and quiet stale alerts."""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'app'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))

import config_store  # noqa: E402
import log_retention  # noqa: E402
import power_manager  # noqa: E402
import web_auth  # noqa: E402
import web_gui  # noqa: E402
from test_onboarding import PASSWORD, make_env, signed_in, token  # noqa: E402

PROXY = '192.0.2.50'


# --- reverse proxies ---------------------------------------------------------------

def proxied(client, path, visitor='198.51.100.7', proto='https', peer=PROXY, **kw):
    headers = {'X-Forwarded-For': visitor, 'X-Forwarded-Proto': proto, 'X-Forwarded-Host': 'ups.example.com'}
    headers.update(kw.pop('headers', {}))
    return client.open(path, headers=headers, environ_base={'REMOTE_ADDR': peer}, **kw)


def test_without_trusted_proxies_the_headers_are_ignored(tmp_path, monkeypatch):
    _, client = make_env(tmp_path, monkeypatch, sentinels='192.0.2.101')
    web_auth.set_password(PASSWORD)
    tok = token(client, '/login')
    # Forged headers from an untrusted address can't dodge the pause...
    for n in range(web_auth.MAX_FAILURES):
        proxied(client, '/login', visitor=f'198.51.100.{n}', method='POST',
                data={'password': 'wrong-guess', 'csrf_token': tok})
    r = proxied(client, '/login', visitor='198.51.100.99', method='POST',
                data={'password': PASSWORD, 'csrf_token': tok})
    assert 'Too many wrong passwords' in r.get_data(as_text=True)


def test_a_trusted_proxy_passes_on_the_real_visitor(tmp_path, monkeypatch):
    _, client = make_env(tmp_path, monkeypatch, sentinels='192.0.2.101')
    web_auth.set_password(PASSWORD)
    web_auth.set_trusted_proxies([PROXY])
    tok = token(client, '/login')
    # A stranger's wrong guesses through the proxy...
    for _ in range(web_auth.MAX_FAILURES):
        proxied(client, '/login', visitor='203.0.113.66', method='POST',
                data={'password': 'wrong-guess', 'csrf_token': tok})
    # ...don't lock out the owner coming through the same proxy.
    r = proxied(client, '/login', visitor='198.51.100.7', method='POST',
                data={'password': PASSWORD, 'csrf_token': tok})
    assert r.status_code == 302
    # Over HTTPS the sign-in cookie is HTTPS-only.
    assert 'Secure' in r.headers.get('Set-Cookie', '')


def test_plain_http_by_ip_still_signs_in(tmp_path, monkeypatch):
    _, client = make_env(tmp_path, monkeypatch, sentinels='192.0.2.101')
    web_auth.set_password(PASSWORD)
    web_auth.set_trusted_proxies([PROXY])
    tok = token(client, '/login')
    r = client.post('/login', data={'password': PASSWORD, 'csrf_token': tok},
                    environ_base={'REMOTE_ADDR': '192.0.2.77'})
    assert r.status_code == 302 and 'Secure' not in r.headers.get('Set-Cookie', '')


def test_chained_proxies_and_ranges(tmp_path, monkeypatch):
    make_env(tmp_path, monkeypatch)
    web_auth.set_trusted_proxies([PROXY, '172.16.0.0/12'])
    seen = {}

    def app(environ, start_response):
        seen.update(addr=environ['REMOTE_ADDR'], scheme=environ['wsgi.url_scheme'], peer=environ['ups.peer_addr'])
        start_response('200 OK', [])
        return [b'']

    mw = web_auth.ProxyAware(app)
    env = {'REMOTE_ADDR': PROXY, 'HTTP_X_FORWARDED_FOR': '198.51.100.7, 172.18.0.1',
           'HTTP_X_FORWARDED_PROTO': 'https', 'wsgi.url_scheme': 'http'}
    mw(env, lambda *a: None)
    assert seen == {'addr': '198.51.100.7', 'scheme': 'https', 'peer': PROXY}
    # Junk in the header leaves the proxy's own address.
    env = {'REMOTE_ADDR': PROXY, 'HTTP_X_FORWARDED_FOR': 'not-an-ip', 'wsgi.url_scheme': 'http'}
    mw(env, lambda *a: None)
    assert seen['addr'] == PROXY


def test_proxy_list_is_saved_from_the_security_tab(tmp_path, monkeypatch):
    _, client = make_env(tmp_path, monkeypatch, sentinels='192.0.2.101')
    signed_in(client)
    tok = token(client, '/config')
    client.post('/security/proxies', data={'csrf_token': tok, 'trusted_proxies': '192.0.2.50\n172.16.0.0/12'})
    assert web_auth.trusted_proxies() == ['192.0.2.50', '172.16.0.0/12']
    client.post('/security/proxies', data={'csrf_token': tok, 'trusted_proxies': 'proxy.lan'})
    assert web_auth.trusted_proxies() == ['192.0.2.50', '172.16.0.0/12']
    html = client.get('/config').get_data(as_text=True)
    assert 'This page reached the server from' in html and '172.16.0.0/12' in html


# --- log retention -------------------------------------------------------------------

def test_old_rotated_logs_are_deleted_and_current_ones_kept(tmp_path):
    now = time.time()
    files = {'power_manager.log': 40, 'power_manager.log.1': 1, 'power_manager.log.9.gz': 9,
             'syslog.20.gz': 20, 'user.log.3.gz': 3, 'notes.txt.30': 30, 'syslog': 40}
    for name, age in files.items():
        p = tmp_path / name
        p.write_text('x')
        os.utime(p, (now - age * 86400, now - age * 86400))
    removed = log_retention.prune(str(tmp_path), 7, now=now)
    assert removed == ['power_manager.log.9.gz', 'syslog.20.gz']
    assert (tmp_path / 'power_manager.log').exists() and (tmp_path / 'syslog').exists()
    assert (tmp_path / 'notes.txt.30').exists()


def test_retention_setting_defaults_to_seven():
    assert log_retention.retention_days({}) == 7
    assert log_retention.retention_days({'LOG_RETENTION_DAYS': '30'}) == 30
    assert log_retention.retention_days({'LOG_RETENTION_DAYS': '1000'}) == 7


def test_keep_logs_for_saves_on_its_own(tmp_path, monkeypatch):
    _, client = make_env(tmp_path, monkeypatch, sentinels='192.0.2.101')
    signed_in(client)
    tok = token(client, '/config')
    headers = {'X-CSRF-Token': tok, 'X-Requested-With': 'fetch'}
    r = client.post('/settings/choice', data={'name': 'LOG_RETENTION_DAYS', 'value': '14'}, headers=headers)
    assert r.get_json() == {'success': True, 'message': 'Logs are now kept for 14 days.'}
    assert web_gui.read_power_manager_config()[0]['LOG_RETENTION_DAYS'] == '14'
    r = client.post('/settings/choice', data={'name': 'LOG_RETENTION_DAYS', 'value': '365'}, headers=headers)
    assert r.status_code == 400
    r = client.post('/settings/choice', data={'name': 'API_TOKEN', 'value': '7'}, headers=headers)
    assert r.status_code == 400


# --- example config ----------------------------------------------------------------------

def test_example_config_has_no_comments_after_values():
    example = os.path.join(ROOT, 'config', 'power_manager.conf.example')
    for line in open(example):
        if re.match(r'^[A-Z_]+=', line):
            assert '#' not in line.split('=', 1)[1], line
    config = config_store.read(example)[0]
    assert config['BATTERY_DEFAULT_CRITICAL_VOLTAGE'] == '11.0'
    assert config['BATTERY_SIM_SOC'] == ''


# --- quiet stale alerts --------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, kind, subject, body):
        self.sent.append(kind)


def stale_check(tmp_path, monkeypatch, status, minutes_ago, power_state=None, confirm=10):
    path = tmp_path / 'client_status.json'
    ts = (datetime.utcnow() - timedelta(minutes=minutes_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')
    path.write_text(json.dumps({'192.0.2.10': {'status': status, 'timestamp': ts}}))
    monkeypatch.setattr(power_manager, 'CLIENT_STATUS_FILE', str(path))
    pm = power_manager.PowerManager.__new__(power_manager.PowerManager)
    pm.config = {'CLIENT_STALE_TIMEOUT_MINUTES': '5', 'WOL_CONFIRM_TIMEOUT_MINUTES': str(confirm)}
    pm.wake_hosts = {'WAKE_HOST_1': {'NAME': 'Server', 'IP': '192.0.2.10', 'SHUTDOWN_DELAY_MINUTES': '0'}}
    pm.power_state = power_state
    pm.client_notification_states = {}
    pm.notifier = Recorder()
    pm._check_client_statuses()
    return pm.notifier.sent


def test_no_stale_email_during_a_power_cut(tmp_path, monkeypatch):
    assert 'CLIENT_STALE' not in stale_check(tmp_path, monkeypatch, 'online', 30, power_state='POWER_FAIL')


def test_no_stale_email_after_it_said_it_was_turning_off(tmp_path, monkeypatch):
    sent = stale_check(tmp_path, monkeypatch, 'shutdown_pending', 30, power_state='POWER_OK')
    assert 'CLIENT_STALE' not in sent


def test_a_woken_machine_gets_time_to_start(tmp_path, monkeypatch):
    assert 'CLIENT_STALE' not in stale_check(tmp_path, monkeypatch, 'wol_sent', 8, power_state='POWER_OK')
    assert 'CLIENT_STALE' in stale_check(tmp_path, monkeypatch, 'wol_sent', 12, power_state='POWER_OK')


def test_a_silent_machine_with_power_on_is_still_reported(tmp_path, monkeypatch):
    assert 'CLIENT_STALE' in stale_check(tmp_path, monkeypatch, 'online', 30, power_state='POWER_OK')
