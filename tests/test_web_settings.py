"""Sign-in, CSRF protection and per-tab settings saves.

Runs the web GUI with Flask's test client against a throwaway config file.
Wake-on-LAN is replaced by a recorder, so nothing is ever sent.
"""

import os
import re
import sys

import pytest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
sys.path.insert(0, APP)

import web_auth  # noqa: E402
import web_gui  # noqa: E402

PASSWORD = 'test-password-1'

CONFIG = '''SENTINEL_HOSTS="192.0.2.101 192.0.2.102"
WOL_DELAY_MINUTES="3"
WOL_CONFIRM_TIMEOUT_MINUTES="10"
CLIENT_STALE_TIMEOUT_MINUTES="5"
DEFAULT_BROADCAST_IP="192.0.2.255"
API_TOKEN="test-token"
POWER_SIMULATION_MODE="false"
DEBUG_MODE="false"
SMTP_SERVER="smtp.example.com"
SMTP_PASSWORD="mail-secret-123"
SMTP_RECIPIENTS="a@example.com"
NOTIFY_POWER_FAIL="true"

[WAKE_HOST_1]
NAME="Proxmox"
IP="192.0.2.10"
MAC="02:00:00:00:00:10"
AUTO_WOL="true"
IGNORE_SIMULATION="false"
SHUTDOWN_DELAY_MINUTES="0"
POWER_SOURCE="battery"
SHUTDOWN_SOC="40"

[SCHEDULE_1]
NAME="Weekly test"
TYPE="recurring"
TIME="03:00"
ACTION="start"
ENABLED="false"
DAY_OF_WEEK="sunday"
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    conf = tmp_path / 'power_manager.conf'
    conf.write_text(CONFIG)
    monkeypatch.setattr(web_gui, 'POWER_MANAGER_CONFIG', str(conf))
    monkeypatch.setattr(web_auth, 'AUTH_FILE', str(tmp_path / 'web_auth.json'))
    monkeypatch.setattr(web_auth, '_cache', {'mtime': None, 'data': {}})
    monkeypatch.setattr(web_auth, '_failures', {})
    sent = []
    monkeypatch.setattr(web_gui, 'send_wol', lambda mac, ip: sent.append((mac, ip)) or True)
    monkeypatch.setattr(web_gui.reachability, 'check', lambda ips, **kw: {ip: True for ip in ips})
    web_gui.app.config['TESTING'] = True
    return {'conf': conf, 'client': web_gui.app.test_client(), 'sent': sent}


def token(client, path='/config'):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def signed_in(env):
    web_auth.set_password(PASSWORD)
    client = env['client']
    tok = token(client, '/login')
    r = client.post('/login', data={'password': PASSWORD, 'csrf_token': tok})
    assert r.status_code == 302
    return client


def parsed(env):
    web_gui.POWER_MANAGER_CONFIG = str(env['conf'])
    return web_gui.read_power_manager_config()


# --- sign-in -----------------------------------------------------------------

def test_first_request_after_start_can_use_the_session(env):
    # A fresh worker has no secret yet; the very first page must still work.
    web_gui.app.secret_key = None
    web_auth.set_password(PASSWORD)
    web_gui.app.secret_key = None
    assert env['client'].get('/login').status_code == 200


def test_first_run_goes_to_setup(env):
    r = env['client'].get('/config')
    assert r.status_code == 302 and r.headers['Location'].endswith('/setup')


def test_setup_creates_password_and_signs_in(env):
    client = env['client']
    tok = token(client, '/setup')
    r = client.post('/setup', data={'password': PASSWORD, 'confirm': PASSWORD, 'csrf_token': tok})
    assert r.status_code == 302
    assert web_auth.has_password()
    assert client.get('/config').status_code == 200
    # The stored value is a hash, never the password itself.
    assert PASSWORD not in open(web_auth.AUTH_FILE).read()


def test_setup_refuses_short_or_mismatched(env):
    client = env['client']
    tok = token(client, '/setup')
    r = client.post('/setup', data={'password': 'short', 'confirm': 'short', 'csrf_token': tok})
    assert 'at least 8' in r.get_data(as_text=True)
    r = client.post('/setup', data={'password': PASSWORD, 'confirm': 'different-1', 'csrf_token': tok})
    assert "don&#39;t match" in r.get_data(as_text=True)
    assert not web_auth.has_password()


def test_signed_out_pages_redirect_to_login(env):
    web_auth.set_password(PASSWORD)
    for path in ('/', '/config', '/dashboard/live', '/status', '/power_state'):
        r = env['client'].get(path)
        assert r.status_code == 302 and '/login' in r.headers['Location'], path


def test_signed_out_fetch_gets_401(env):
    web_auth.set_password(PASSWORD)
    r = env['client'].get('/dashboard/live', headers={'X-Requested-With': 'fetch'})
    assert r.status_code == 401


def test_wrong_password_then_lockout(env):
    web_auth.set_password(PASSWORD)
    client = env['client']
    tok = token(client, '/login')
    for _ in range(web_auth.MAX_FAILURES):
        r = client.post('/login', data={'password': 'nope-nope', 'csrf_token': tok})
    assert 'Too many wrong passwords' in r.get_data(as_text=True)
    # Even the right password is refused while locked.
    r = client.post('/login', data={'password': PASSWORD, 'csrf_token': tok})
    assert 'Too many wrong passwords' in r.get_data(as_text=True)


def test_login_never_redirects_off_site(env):
    web_auth.set_password(PASSWORD)
    client = env['client']
    tok = token(client, '/login')
    r = client.post('/login', data={'password': PASSWORD, 'csrf_token': tok, 'next': '//evil.example/'})
    assert r.headers['Location'] == '/'


def test_changing_password_signs_out_other_devices(env):
    client = signed_in(env)
    other = web_gui.app.test_client()
    tok = token(other, '/login')
    other.post('/login', data={'password': PASSWORD, 'csrf_token': tok})
    assert other.get('/config').status_code == 200

    tok = token(client)
    client.post('/security/password', data={'current': PASSWORD, 'password': 'new-password-2',
                                            'confirm': 'new-password-2', 'csrf_token': tok})
    assert client.get('/config').status_code == 200
    assert other.get('/config').status_code == 302


# --- CSRF --------------------------------------------------------------------

def test_post_without_token_changes_nothing(env):
    client = signed_in(env)
    before = env['conf'].read_text()
    r = client.post('/settings/test_mode', data={'state': 'on'})
    assert r.status_code == 302
    assert env['conf'].read_text() == before


def test_fetch_without_token_is_refused(env):
    client = signed_in(env)
    r = client.post('/wol/WAKE_HOST_1', headers={'X-Requested-With': 'fetch'})
    assert r.status_code == 400
    assert env['sent'] == []


def test_wake_needs_post_and_token(env):
    client = signed_in(env)
    assert client.get('/wol/WAKE_HOST_1').status_code == 405
    tok = token(client)
    r = client.post('/wol/WAKE_HOST_1', headers={'X-CSRF-Token': tok, 'X-Requested-With': 'fetch'})
    assert r.get_json()['success']
    assert env['sent'] == [('02:00:00:00:00:10', '192.0.2.255')]


# --- per-tab saves -------------------------------------------------------------

def test_general_save_touches_only_general_keys(env):
    client = signed_in(env)
    tok = token(client)
    before_cfg, before_hosts, before_sched = parsed(env)
    client.post('/settings/general', data={
        'csrf_token': tok, 'sentinel_hosts': '192.0.2.101\n192.0.2.103', 'wol_delay_minutes': '4',
        'wol_confirm_timeout_minutes': '10', 'client_stale_timeout_minutes': '5',
        'default_broadcast_ip': '192.0.2.255'})
    cfg, hosts, sched = parsed(env)
    assert cfg['SENTINEL_HOSTS'] == '192.0.2.101 192.0.2.103'
    assert cfg['WOL_DELAY_MINUTES'] == '4'
    # Everything else is exactly as before.
    for key in ('POWER_SIMULATION_MODE', 'SMTP_PASSWORD', 'SMTP_SERVER', 'NOTIFY_POWER_FAIL', 'API_TOKEN'):
        assert cfg[key] == before_cfg[key], key
    assert hosts == before_hosts
    assert sched == before_sched


def test_general_save_rejects_bad_input(env):
    client = signed_in(env)
    tok = token(client)
    before = env['conf'].read_text()
    client.post('/settings/general', data={
        'csrf_token': tok, 'sentinel_hosts': '192.0.2.101 not-an-ip', 'wol_delay_minutes': '4',
        'wol_confirm_timeout_minutes': '10', 'client_stale_timeout_minutes': '5',
        'default_broadcast_ip': '192.0.2.255'})
    assert env['conf'].read_text() == before


def test_test_mode_only_changes_through_its_own_route(env):
    client = signed_in(env)
    tok = token(client)
    client.post('/settings/test_mode', data={'state': 'on', 'csrf_token': tok})
    assert parsed(env)[0]['POWER_SIMULATION_MODE'] == 'true'
    # Saving another tab leaves it on.
    client.post('/settings/notifications', data={'csrf_token': tok, 'smtp_server': 'smtp.example.com'})
    assert parsed(env)[0]['POWER_SIMULATION_MODE'] == 'true'
    client.post('/settings/test_mode', data={'state': 'off', 'csrf_token': tok})
    assert parsed(env)[0]['POWER_SIMULATION_MODE'] == 'false'


def test_email_password_never_reaches_the_browser(env):
    client = signed_in(env)
    html = client.get('/config').get_data(as_text=True)
    assert 'mail-secret-123' not in html
    assert 'Saved — leave blank to keep it' in html


def test_blank_email_password_keeps_it_and_clear_removes_it(env):
    client = signed_in(env)
    tok = token(client)
    client.post('/settings/notifications', data={'csrf_token': tok, 'smtp_server': 'smtp.example.com',
                                                 'smtp_password': ''})
    assert parsed(env)[0]['SMTP_PASSWORD'] == 'mail-secret-123'
    client.post('/settings/notifications', data={'csrf_token': tok, 'smtp_server': 'smtp.example.com',
                                                 'smtp_password': 'new-mail-pass'})
    assert parsed(env)[0]['SMTP_PASSWORD'] == 'new-mail-pass'
    client.post('/settings/notifications', data={'csrf_token': tok, 'smtp_server': 'smtp.example.com',
                                                 'clear_smtp_password': '1'})
    assert 'SMTP_PASSWORD' not in parsed(env)[0]


def test_editing_a_machine_keeps_its_battery_rules(env):
    client = signed_in(env)
    tok = token(client)
    client.post('/edit_wake_host/WAKE_HOST_1', data={
        'csrf_token': tok, 'name': 'Proxmox host', 'ip': '192.0.2.10', 'mac': '02:00:00:00:00:10',
        'shutdown_delay': '0', 'auto_wol': 'true'})
    _, hosts, sched = parsed(env)
    host = hosts['WAKE_HOST_1']
    assert host['NAME'] == 'Proxmox host'
    assert host['POWER_SOURCE'] == 'battery' and host['SHUTDOWN_SOC'] == '40'
    assert 'SCHEDULE_1' in sched


def test_incomplete_schedule_is_refused(env):
    client = signed_in(env)
    tok = token(client)
    client.post('/add_schedule', data={'csrf_token': tok, 'name': 'No time', 'type': 'recurring',
                                       'day_of_week': 'sunday', 'action': 'start'})
    assert list(parsed(env)[2]) == ['SCHEDULE_1']
