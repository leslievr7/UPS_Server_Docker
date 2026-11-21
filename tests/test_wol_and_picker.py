"""Wake-on-LAN support reported by the UPS script, and who can be a sentinel."""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'app'))
sys.path.insert(0, os.path.join(ROOT, 'dev'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))

import client_status  # noqa: E402
import machine_roles  # noqa: E402
import network_scan  # noqa: E402
import web_gui  # noqa: E402
import test_onboarding as ob  # noqa: E402
from test_keep_running import AUTH, api_client  # noqa: E402,F401
from test_onboarding import scan_files  # noqa: E402,F401
from test_web_settings import env, signed_in, token  # noqa: E402,F401

PI_MAC = 'DC:A6:32:00:00:30'


# --- what the script reports ------------------------------------------------------

def test_check_ins_record_wake_on_lan_support(api_client):
    api_client.post('/status', headers=AUTH, json={'ip': '192.0.2.10', 'status': 'online',
                                                   'client_version': '4.5.0', 'wol': 'off'})
    api_client.post('/status', headers=AUTH, json={'ip': '192.0.2.12', 'status': 'online',
                                                   'client_version': '4.5.0', 'wol': 'maybe'})
    saved = json.loads((api_client.tmp / 'client_status.json').read_text())
    assert saved['192.0.2.10']['wol'] == 'off'
    assert 'wol' not in saved['192.0.2.12']


def test_marking_a_machine_as_woken_keeps_what_its_script_said(tmp_path):
    path = str(tmp_path / 'client_status.json')
    client_status.update(path, '192.0.2.10', {'status': 'online', 'client_version': '4.5.0', 'wol': 'on'})
    client_status.update(path, '192.0.2.10', {'status': 'wol_sent'}, keep=('client_version', 'wol'))
    record = json.load(open(path))['192.0.2.10']
    assert record == {'status': 'wol_sent', 'client_version': '4.5.0', 'wol': 'on'}
    # Without keep, an entry is replaced as before.
    client_status.update(path, '192.0.2.10', {'status': 'online'})
    assert json.load(open(path))['192.0.2.10'] == {'status': 'online'}


def test_wake_on_lan_states():
    prefixes = machine_roles.no_wake_prefixes()
    state = machine_roles.wol_state
    assert state('02:00:00:00:00:10', {'wol': 'on'}, prefixes) is None
    assert state('02:00:00:00:00:10', None, prefixes) is None
    assert state('02:00:00:00:00:10', {'wol': 'off'}, prefixes) == 'off'
    assert state('02:00:00:00:00:10', {'wol': 'unsupported'}, prefixes) == 'unsupported'
    # A Raspberry Pi's MAC: unsure, unless its script says the card can't do it.
    assert state(PI_MAC, None, prefixes) == 'unsure'
    assert state(PI_MAC, {'wol': 'on'}, prefixes) == 'unsure'
    assert state(PI_MAC, {'wol': 'unsupported'}, prefixes) == 'unsupported'


# --- settings and dashboard -------------------------------------------------------

def add_pi(env):
    with open(env['conf'], 'a') as f:
        f.write(f'\n[WAKE_HOST_2]\nNAME="Pi NAS"\nIP="192.0.2.30"\nMAC="{PI_MAC}"\nAUTO_WOL="true"\n')


def with_statuses(tmp_path, monkeypatch, records):
    path = tmp_path / 'client_status.json'
    path.write_text(json.dumps(records))
    monkeypatch.setattr(web_gui, 'CLIENT_STATUS_FILE', str(path))


def test_settings_list_shows_wake_on_lan_badges(env, tmp_path, monkeypatch):
    add_pi(env)
    with_statuses(tmp_path, monkeypatch, {'192.0.2.10': {'status': 'online', 'client_version': '4.5.0',
                                                         'wol': 'off', 'timestamp': '2026-01-01T00:00:00Z'}})
    html = signed_in(env).get('/config').get_data(as_text=True)
    assert 'WoL is off' in html and 'WoL support unsure' in html
    # The pop-up gets the reports and the sentinels, to warn while typing.
    assert 'var wolReports = {"192.0.2.10": "off"};' in html
    assert 'var sentinelIps = ["192.0.2.101", "192.0.2.102"];' in html
    assert 'This machine is added as a sentinel.' in html


def test_dashboard_shows_wake_on_lan_notes(env, tmp_path, monkeypatch):
    add_pi(env)
    with_statuses(tmp_path, monkeypatch, {'192.0.2.10': {'status': 'online', 'client_version': '4.5.0',
                                                         'wol': 'off', 'timestamp': '2026-01-01T00:00:00Z'}})
    html = signed_in(env).get('/').get_data(as_text=True)
    assert '<span class="wol-note" data-tone="warning">WoL is off</span>' in html
    assert '<span class="wol-note" data-tone="faint">WoL support unsure</span>' in html
    assert 'the server wakes the ones set to wake up, if they support WoL' in html


def test_a_sentinel_that_is_a_machine_is_pointed_out_on_save(env):
    client = signed_in(env)
    tok = token(client)
    client.post('/settings/general', data={
        'csrf_token': tok, 'sentinel_hosts': '192.0.2.101\n192.0.2.10', 'wol_delay_minutes': '3',
        'wol_confirm_timeout_minutes': '10', 'client_stale_timeout_minutes': '5',
        'default_broadcast_ip': '192.0.2.255'})
    html = client.get('/config').get_data(as_text=True)
    assert '192.0.2.10 is added as a Machine.' in html


# --- the device picker ------------------------------------------------------------

HOSTS = {'WAKE_HOST_1': {'NAME': 'Proxmox', 'IP': '192.0.2.10'},
         'WAKE_HOST_2': {'NAME': 'NAS', 'IP': '192.0.2.27'}}


def test_who_can_be_picked(scan_files):
    state = ob.run_fake_scan()
    # The NAS at .27 is a machine but already a sentinel; Proxmox at .10 isn't.
    groups = network_scan.describe(state, {'SENTINEL_HOSTS': '192.0.2.27'}, HOSTS)
    by_ip = {e['ip']: e for e in groups['good'] + groups['unsuitable']}
    assert by_ip['192.0.2.2']['reason'] == 'UPS Server runs here' and by_ip['192.0.2.2']['locked']
    assert by_ip['192.0.2.10']['reason'] == 'Added as Machine' and by_ip['192.0.2.10']['locked']
    # Already a sentinel: still in the list, ticked, and can be unticked.
    nas = by_ip['192.0.2.27']
    assert nas['current'] and nas['reason'] == 'Added as Machine' and not nas['locked']
    assert nas in groups['good']
    # "Probably not suitable" devices can be chosen.
    assert by_ip['192.0.2.1']['reason'] == 'Your router' and not by_ip['192.0.2.1']['locked']
    assert not by_ip['192.0.2.40']['locked']


def test_saving_refuses_the_server_and_machines(tmp_path, monkeypatch, scan_files):
    conf, client = ob.make_env(tmp_path, monkeypatch)
    ob.signed_in(client)
    ob.run_fake_scan()
    tok = ob.token(client, '/sentinels/find?guide=skip')
    before = conf.read_text()
    for ip in ('192.0.2.2', '192.0.2.10'):
        client.post('/sentinels/save', data={'csrf_token': tok, 'ip': ['192.0.2.20', ip]})
        assert conf.read_text() == before
    # The router and a phone are allowed, if that's what someone wants.
    client.post('/sentinels/save', data={'csrf_token': tok, 'ip': ['192.0.2.20', '192.0.2.1', '192.0.2.40']})
    config, _, _ = web_gui.read_power_manager_config()
    assert config['SENTINEL_HOSTS'] == '192.0.2.20 192.0.2.1 192.0.2.40'


def test_picker_page_wording(tmp_path, monkeypatch, scan_files):
    _, client = ob.make_env(tmp_path, monkeypatch)
    ob.signed_in(client)
    html = client.get('/sentinels/find?guide=skip').get_data(as_text=True)
    assert 'Add an address manually' in html and 'by hand' not in html
    assert 'Probably not suitable' in html
    assert 'You can still select them, except the UPS Server and devices added as Machines.' in html
    assert re.search(r'var MACHINES = \["192\.0\.2\.10"\];', html)
