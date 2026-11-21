"""First-run sentinel setup, the network scan and the device picker.

The scan runs against dev/fake_lan.py's pretend network, so nothing is sent.
"""

import os
import re
import socket
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'app'))
sys.path.insert(0, os.path.join(ROOT, 'dev'))

import fake_lan  # noqa: E402
import network_scan  # noqa: E402
import web_auth  # noqa: E402
import web_gui  # noqa: E402

PASSWORD = 'test-password-1'

CONFIG = '''# kept comment
SENTINEL_HOSTS="{sentinels}"
WOL_DELAY_MINUTES="3"
WOL_CONFIRM_TIMEOUT_MINUTES="10"
CLIENT_STALE_TIMEOUT_MINUTES="5"
DEFAULT_BROADCAST_IP="192.0.2.255"
API_TOKEN="test-token"
POWER_SIMULATION_MODE="false"

[WAKE_HOST_1]
NAME="Proxmox"
IP="192.0.2.10"
MAC="02:00:00:00:00:10"
'''


@pytest.fixture
def scan_files(tmp_path, monkeypatch):
    monkeypatch.setattr(network_scan, 'SCAN_FILE', str(tmp_path / 'scan.json'))
    monkeypatch.setattr(network_scan, 'LOCK_FILE', str(tmp_path / 'scan.lock'))
    monkeypatch.setattr(network_scan, 'FIRST_WAIT', 0.05)
    monkeypatch.setattr(network_scan, 'SECOND_WAIT', 0.05)
    monkeypatch.setattr(network_scan, 'NAME_WAIT', 1.0)
    saved = dict(network_scan.backend)
    fake_lan.install(network_scan, reachable=lambda: fake_lan.answering(True) + ['192.0.2.10'],
                     machine_macs=lambda: {'192.0.2.10': '02:00:00:00:00:10'})
    yield tmp_path
    network_scan.backend.clear()
    network_scan.backend.update(saved)


def make_env(tmp_path, monkeypatch, sentinels=''):
    conf = tmp_path / 'power_manager.conf'
    conf.write_text(CONFIG.format(sentinels=sentinels))
    monkeypatch.setattr(web_gui, 'POWER_MANAGER_CONFIG', str(conf))
    monkeypatch.setattr(web_auth, 'AUTH_FILE', str(tmp_path / 'web_auth.json'))
    monkeypatch.setattr(web_auth, '_cache', {'mtime': None, 'data': {}})
    monkeypatch.setattr(web_auth, '_failures', {})
    # No real pings: addresses answer as they would on the pretend network.
    answering = set(fake_lan.answering(True)) | {'192.0.2.5', '192.0.2.6', '192.0.2.60'}
    monkeypatch.setattr(web_gui.reachability, 'check', lambda ips, **kw: {ip: ip in answering for ip in ips})
    web_gui.app.config['TESTING'] = True
    return conf, web_gui.app.test_client()


def token(client, path):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def signed_in(client):
    web_auth.set_password(PASSWORD)
    tok = token(client, '/login')
    client.post('/login', data={'password': PASSWORD, 'csrf_token': tok})
    return client


# --- the ping packet -------------------------------------------------------------

def test_echo_request_has_a_valid_checksum():
    packet = network_scan.echo_request(0x1234, 7)
    assert network_scan._checksum(packet) == 0
    assert packet[0] == 8  # echo request


def test_only_our_echo_replies_count():
    reply = bytearray(network_scan.echo_request(0x1234, 7))
    reply[0] = 0  # echo reply
    ip_header = bytes([0x45]) + bytes(11) + socket.inet_aton('192.0.2.20') + bytes(4)
    assert network_scan.parse_reply(ip_header + bytes(reply), 0x1234, True) == '192.0.2.20'
    assert network_scan.parse_reply(ip_header + bytes(reply), 0x9999, True) is None
    request = ip_header + network_scan.echo_request(0x1234, 7)
    assert network_scan.parse_reply(request, 0x1234, True) is None


def test_scans_the_servers_own_neighbourhood_only():
    ips = network_scan.addresses_to_scan('192.0.2.2')
    assert len(ips) == 253 and '192.0.2.2' not in ips
    assert ips[0] == '192.0.2.1' and ips[-1] == '192.0.2.254'


def test_private_macs_are_phones_and_laptops():
    assert network_scan.is_private_mac('da:7e:91:00:00:01')
    assert network_scan.is_private_mac('02:00:00:00:00:10')
    assert not network_scan.is_private_mac('00:4b:12:10:00:20')
    assert not network_scan.is_private_mac('')


# --- a whole scan against the pretend network ------------------------------------

def run_fake_scan():
    return network_scan.run_scan()


def test_scan_finds_devices_with_macs_names_and_makers(scan_files):
    state = run_fake_scan()
    assert state['status'] == 'done'
    devices = state['devices']
    esp = devices['192.0.2.20']
    assert esp['mac'] == '00:4b:12:10:00:20' and esp['maker'] == 'Espressif' and esp['kind'] == 'smart'
    assert esp['name'] == 'ESP_3A1B2C'
    # The printer answers "who has this address?" but not pings.
    assert devices['192.0.2.26']['replied'] is False
    assert devices['192.0.2.26']['maker'] == 'Epson'
    assert '192.0.2.99' not in devices
    assert network_scan.status()['status'] == 'done'


def test_scan_sorts_good_choices_from_unsuitable(scan_files):
    state = run_fake_scan()
    groups = network_scan.describe(state, {'SENTINEL_HOSTS': ''},
                                   {'WAKE_HOST_1': {'NAME': 'Proxmox', 'IP': '192.0.2.10'}})
    reasons = {e['ip']: e['reason'] for e in groups['unsuitable']}
    # The scan skips the server's own address, but the picker lists it, locked.
    assert reasons['192.0.2.2'] == 'UPS Server runs here'
    assert reasons['192.0.2.1'] == 'Your router'
    assert reasons['192.0.2.10'] == 'Added as Machine'
    assert reasons['192.0.2.40'] == 'Phone or laptop'      # private MAC
    assert reasons['192.0.2.42'] == 'Phone or laptop'      # name says laptop
    assert reasons['192.0.2.26'] == "Doesn't answer pings"
    good = [e['ip'] for e in groups['good']]
    assert '192.0.2.20' in good and '192.0.2.101' in good
    # Smart-home devices come before the NAS.
    assert good.index('192.0.2.20') < good.index('192.0.2.27')
    assert not set(good) & set(reasons)


def test_current_sentinels_are_ticked_even_when_silent(scan_files):
    state = run_fake_scan()
    groups = network_scan.describe(state, {'SENTINEL_HOSTS': '192.0.2.101 192.0.2.77'}, {})
    by_ip = {e['ip']: e for e in groups['good']}
    assert by_ip['192.0.2.101']['current'] and not by_ip['192.0.2.101']['missing']
    assert by_ip['192.0.2.77']['current'] and by_ip['192.0.2.77']['missing']
    assert {e['ip'] for e in groups['good'][:2]} == {'192.0.2.77', '192.0.2.101'}


def test_one_scan_at_a_time_and_recent_results_reused(scan_files):
    held = network_scan._try_lock()
    try:
        assert network_scan.start() == 'running'
        assert network_scan.is_running()
    finally:
        held.close()
    run_fake_scan()
    assert network_scan.start() == 'recent'


def test_a_scan_whose_worker_died_reads_as_failed(scan_files):
    network_scan._write_state({'status': 'running', 'devices': {}})
    assert network_scan.status()['status'] == 'failed'


def test_example_sentinels_count_as_none():
    assert network_scan.configured_sentinels({'SENTINEL_HOSTS': network_scan.EXAMPLE_SENTINELS}) == []
    assert network_scan.configured_sentinels({'SENTINEL_HOSTS': '192.0.2.5,192.0.2.6'}) == ['192.0.2.5', '192.0.2.6']


# --- remembering sentinels by MAC -----------------------------------------------------

def test_address_changes():
    config = {'SENTINEL_HOSTS': '192.0.2.5 192.0.2.6 192.0.2.7 192.0.2.8',
              'SENTINEL_MACS': '192.0.2.5=00:4b:12:00:00:05 192.0.2.6=00:4b:12:00:00:06 '
                               '192.0.2.7=00:4b:12:00:00:07'}
    table = {
        '192.0.2.5': '00:4b:12:00:00:05',          # unchanged
        '192.0.2.6': 'da:00:00:00:00:99',          # someone else has it now
        '192.0.2.60': '00:4b:12:00:00:06',         # ...and the plug moved here
        '192.0.2.70': '00:4b:12:00:00:07',         # 7 is silent but its device is at .70
    }
    changes = {c['ip']: c for c in network_scan.address_changes(config, table)}
    assert set(changes) == {'192.0.2.6', '192.0.2.7'}
    assert changes['192.0.2.6'] == {'ip': '192.0.2.6', 'new_ip': '192.0.2.60', 'replaced': True}
    assert changes['192.0.2.7'] == {'ip': '192.0.2.7', 'new_ip': '192.0.2.70', 'replaced': False}
    assert network_scan.address_changes({'SENTINEL_HOSTS': '192.0.2.5'}, table) == []


def test_sentinel_macs_round_trip():
    macs = {'192.0.2.5': '00:4b:12:00:00:05', '192.0.2.6': '00:04:3c:00:00:06'}
    assert network_scan.parse_sentinel_macs(network_scan.format_sentinel_macs(macs)) == macs


# --- the first-run flow -------------------------------------------------------------

def test_new_install_goes_from_password_to_the_welcome_step(tmp_path, monkeypatch):
    _, client = make_env(tmp_path, monkeypatch)
    tok = token(client, '/setup')
    r = client.post('/setup', data={'password': PASSWORD, 'confirm': PASSWORD, 'csrf_token': tok})
    assert r.headers['Location'].endswith('/welcome')
    assert 'Watch for power cuts?' in client.get('/welcome').get_data(as_text=True)


def test_install_with_sentinels_skips_the_welcome_step(tmp_path, monkeypatch):
    _, client = make_env(tmp_path, monkeypatch, sentinels='192.0.2.101 192.0.2.102')
    tok = token(client, '/setup')
    r = client.post('/setup', data={'password': PASSWORD, 'confirm': PASSWORD, 'csrf_token': tok})
    assert r.headers['Location'] == '/'
    assert client.get('/welcome').headers['Location'] == '/'


def test_example_sentinels_still_get_the_welcome_step(tmp_path, monkeypatch):
    _, client = make_env(tmp_path, monkeypatch, sentinels=network_scan.EXAMPLE_SENTINELS)
    tok = token(client, '/setup')
    r = client.post('/setup', data={'password': PASSWORD, 'confirm': PASSWORD, 'csrf_token': tok})
    assert r.headers['Location'].endswith('/welcome')


def test_later_is_remembered(tmp_path, monkeypatch):
    _, client = make_env(tmp_path, monkeypatch)
    signed_in(client)
    tok = token(client, '/welcome')
    r = client.post('/welcome/later', data={'csrf_token': tok})
    assert r.headers['Location'] == '/'
    assert client.get('/welcome').headers['Location'] == '/'
    assert web_auth.get_flag('onboarding_done')
    # The dashboard still offers to find devices.
    assert '/sentinels/find' in client.get('/').get_data(as_text=True)


def test_guide_is_shown_once(tmp_path, monkeypatch):
    _, client = make_env(tmp_path, monkeypatch)
    signed_in(client)
    r = client.get('/sentinels/find')
    assert r.status_code == 302 and '/sentinels/guide' in r.headers['Location']
    assert 'What makes a good sentinel' in client.get('/sentinels/guide').get_data(as_text=True)
    assert client.get('/sentinels/find?guide=skip').status_code == 200
    assert client.get('/sentinels/find').status_code == 200
    assert client.get('/sentinels/find?from=settings').status_code == 200


def test_flags_survive_a_password_change(tmp_path, monkeypatch):
    make_env(tmp_path, monkeypatch)
    web_auth.set_password(PASSWORD)
    web_auth.set_flag('onboarding_done')
    web_auth.set_password('another-password-2')
    assert web_auth.get_flag('onboarding_done')


# --- scanning and saving through the web pages -----------------------------------------

def test_starting_a_scan_needs_the_form_token(tmp_path, monkeypatch, scan_files):
    _, client = make_env(tmp_path, monkeypatch)
    signed_in(client)
    started = []
    monkeypatch.setattr(network_scan, 'start', lambda force=False: started.append(force) or 'started')
    assert client.post('/sentinels/scan', headers={'X-Requested-With': 'fetch'}).status_code == 400
    tok = token(client, '/sentinels/find?guide=skip')
    r = client.post('/sentinels/scan?again=1', headers={'X-CSRF-Token': tok, 'X-Requested-With': 'fetch'})
    assert r.get_json() == {'started': 'started'} and started == [True]


def test_scan_status_is_sorted_for_the_picker(tmp_path, monkeypatch, scan_files):
    _, client = make_env(tmp_path, monkeypatch)
    signed_in(client)
    run_fake_scan()
    data = client.get('/sentinels/scan', headers={'X-Requested-With': 'fetch'}).get_json()
    assert data['status'] == 'done' and data['found'] > 10
    assert any(e['ip'] == '192.0.2.20' for e in data['good'])
    assert any(e['ip'] == '192.0.2.10' and e['reason'] == 'Added as Machine' for e in data['unsuitable'])


def test_saving_sentinels_remembers_their_devices(tmp_path, monkeypatch, scan_files):
    conf, client = make_env(tmp_path, monkeypatch)
    signed_in(client)
    run_fake_scan()
    tok = token(client, '/sentinels/find?guide=skip')
    r = client.post('/sentinels/save', data={'csrf_token': tok,
                                             'ip': ['192.0.2.20', '192.0.2.23', '192.0.2.40', '192.0.2.88']})
    assert r.headers['Location'] == '/'
    config, hosts, _ = web_gui.read_power_manager_config()
    assert config['SENTINEL_HOSTS'] == '192.0.2.20 192.0.2.23 192.0.2.40 192.0.2.88'
    macs = network_scan.parse_sentinel_macs(config['SENTINEL_MACS'])
    # The phone's private MAC changes, and .88 was added by hand: neither is remembered.
    assert macs == {'192.0.2.20': '00:4b:12:10:00:20', '192.0.2.23': '00:0d:4b:10:00:23'}
    assert '# kept comment' in conf.read_text() and hosts['WAKE_HOST_1']['NAME'] == 'Proxmox'
    assert web_auth.get_flag('onboarding_done')
    html = client.get('/').get_data(as_text=True)
    assert 'Watching 4 sentinels' in html
    assert "192.0.2.88 didn&#39;t answer just now" in html


def test_saving_from_settings_goes_back_to_settings(tmp_path, monkeypatch, scan_files):
    _, client = make_env(tmp_path, monkeypatch, sentinels='192.0.2.101')
    signed_in(client)
    tok = token(client, '/config')
    r = client.post('/sentinels/save?from=settings', data={'csrf_token': tok, 'ip': ['192.0.2.101']})
    assert r.headers['Location'] == '/config#general'


def test_bad_or_empty_choices_save_nothing(tmp_path, monkeypatch, scan_files):
    conf, client = make_env(tmp_path, monkeypatch, sentinels='192.0.2.101')
    signed_in(client)
    tok = token(client, '/config')
    before = conf.read_text()
    client.post('/sentinels/save', data={'csrf_token': tok, 'ip': ['192.0.2.20', 'not-an-ip']})
    client.post('/sentinels/save', data={'csrf_token': tok})
    assert conf.read_text() == before


def test_following_a_moved_sentinel(tmp_path, monkeypatch):
    conf, client = make_env(tmp_path, monkeypatch, sentinels='192.0.2.5 192.0.2.6')
    with open(conf, 'a') as f:
        f.write('')
    config_text = conf.read_text().replace(
        'WOL_DELAY_MINUTES="3"',
        'WOL_DELAY_MINUTES="3"\nSENTINEL_MACS="192.0.2.5=00:4b:12:00:00:05 192.0.2.6=00:4b:12:00:00:06"')
    conf.write_text(config_text)
    signed_in(client)
    monkeypatch.setitem(network_scan.backend, 'arp_table', lambda: {
        '192.0.2.5': '00:4b:12:00:00:05', '192.0.2.60': '00:4b:12:00:00:06'})
    html = client.get('/').get_data(as_text=True)
    assert 'Sentinel 192.0.2.6 has moved' in html and 'Watch 192.0.2.60 instead' in html
    tok = token(client, '/config')
    client.post('/sentinels/move', data={'csrf_token': tok, 'ip': '192.0.2.6', 'new_ip': '192.0.2.60'})
    config = web_gui.read_power_manager_config()[0]
    assert config['SENTINEL_HOSTS'] == '192.0.2.5 192.0.2.60'
    assert network_scan.parse_sentinel_macs(config['SENTINEL_MACS'])['192.0.2.60'] == '00:4b:12:00:00:06'
    assert 'has moved' not in client.get('/').get_data(as_text=True)


def test_removing_a_sentinel_in_settings_forgets_its_device(tmp_path, monkeypatch):
    conf, client = make_env(tmp_path, monkeypatch, sentinels='192.0.2.5 192.0.2.6')
    conf.write_text(conf.read_text().replace(
        'WOL_DELAY_MINUTES="3"',
        'WOL_DELAY_MINUTES="3"\nSENTINEL_MACS="192.0.2.5=00:4b:12:00:00:05 192.0.2.6=00:4b:12:00:00:06"'))
    signed_in(client)
    tok = token(client, '/config')
    client.post('/settings/general', data={
        'csrf_token': tok, 'sentinel_hosts': '192.0.2.5', 'wol_delay_minutes': '3',
        'wol_confirm_timeout_minutes': '10', 'client_stale_timeout_minutes': '5',
        'default_broadcast_ip': '192.0.2.255'})
    config = web_gui.read_power_manager_config()[0]
    assert network_scan.parse_sentinel_macs(config['SENTINEL_MACS']) == {'192.0.2.5': '00:4b:12:00:00:05'}


def test_banner_offers_to_find_devices_when_nothing_is_watched():
    import dashboard_view
    banner = dashboard_view.build_banner({'status': 'OL'}, False, {}, [], 0, 0, 1, '', None)
    assert banner['action'] == 'find_sentinels'


def test_sentinels_that_all_stay_silent_are_refused(tmp_path, monkeypatch, scan_files):
    # Saving them would look exactly like a power cut and shut machines down.
    conf, client = make_env(tmp_path, monkeypatch, sentinels='192.0.2.101')
    signed_in(client)
    tok = token(client, '/config')
    before = conf.read_text()
    r = client.post('/sentinels/save', data={'csrf_token': tok, 'ip': ['192.0.2.26', '192.0.2.88']})
    assert '/sentinels/find' in r.headers['Location']
    assert conf.read_text() == before
    client.post('/settings/general', data={
        'csrf_token': tok, 'sentinel_hosts': '192.0.2.88', 'wol_delay_minutes': '3',
        'wol_confirm_timeout_minutes': '10', 'client_stale_timeout_minutes': '5',
        'default_broadcast_ip': '192.0.2.255'})
    assert conf.read_text() == before


def test_an_unchanged_list_saves_even_when_silent(tmp_path, monkeypatch):
    # During a real power cut the sentinels are silent; other settings must still save.
    conf, client = make_env(tmp_path, monkeypatch, sentinels='192.0.2.88')
    signed_in(client)
    tok = token(client, '/config')
    client.post('/settings/general', data={
        'csrf_token': tok, 'sentinel_hosts': '192.0.2.88', 'wol_delay_minutes': '7',
        'wol_confirm_timeout_minutes': '10', 'client_stale_timeout_minutes': '5',
        'default_broadcast_ip': '192.0.2.255'})
    assert web_gui.read_power_manager_config()[0]['WOL_DELAY_MINUTES'] == '7'


def test_unfinished_onboarding_comes_back(tmp_path, monkeypatch):
    # Left mid-way: the dashboard and Settings bring the first step back...
    _, client = make_env(tmp_path, monkeypatch)
    signed_in(client)
    assert client.get('/').headers['Location'].endswith('/welcome')
    assert client.get('/config').headers['Location'].endswith('/welcome')
    # ...until they choose Later.
    client.post('/welcome/later', data={'csrf_token': token(client, '/welcome')})
    assert client.get('/').status_code == 200
    assert client.get('/config').status_code == 200


def test_first_run_screens_hide_the_navigation(tmp_path, monkeypatch):
    _, client = make_env(tmp_path, monkeypatch)
    signed_in(client)
    for path in ('/welcome', '/sentinels/guide', '/sentinels/find?guide=skip'):
        html = client.get(path).get_data(as_text=True)
        assert 'aria-label="Main"' not in html, path
        assert 'aria-label="Log out"' in html, path
    # Opened later from Settings, the picker is a normal page with the navigation.
    client.post('/welcome/later', data={'csrf_token': token(client, '/welcome')})
    assert 'aria-label="Main"' in client.get('/sentinels/find?from=settings').get_data(as_text=True)


def test_messages_are_toasts_and_problems_stay_until_closed(tmp_path, monkeypatch):
    _, client = make_env(tmp_path, monkeypatch, sentinels='192.0.2.101')
    signed_in(client)
    tok = token(client, '/config')
    client.post('/settings/general', data={
        'csrf_token': tok, 'sentinel_hosts': '192.0.2.101', 'wol_delay_minutes': '3',
        'wol_confirm_timeout_minutes': '10', 'client_stale_timeout_minutes': '5',
        'default_broadcast_ip': '192.0.2.255'})
    html = client.get('/config').get_data(as_text=True)
    assert re.search(r'class="toast"[^>]*data-category="success" data-duration="5000"', html)
    client.post('/settings/general', data={'csrf_token': tok, 'sentinel_hosts': 'nope'})
    html = client.get('/config').get_data(as_text=True)
    assert re.search(r'class="toast" role="alert"[^>]*data-category="error" data-duration="-1"', html)
