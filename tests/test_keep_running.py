"""Machines that keep running in a power cut, older UPS scripts, and Raspberry Pis."""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.datastructures import MultiDict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'app'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))

import api  # noqa: E402
import dashboard_view  # noqa: E402
import machine_roles  # noqa: E402
import power_manager  # noqa: E402
import web_gui  # noqa: E402
from test_small_fixes import Recorder  # noqa: E402
from test_web_settings import env, parsed, signed_in, token  # noqa: E402,F401

TOKEN = 'keep-running-token'
AUTH = {'Authorization': f'Bearer {TOKEN}'}

API_CONFIG = f'''API_TOKEN="{TOKEN}"
POWER_SIMULATION_MODE="false"

[WAKE_HOST_1]
NAME="Proxmox"
IP="192.0.2.10"
MAC="02:00:00:00:00:10"
SHUTDOWN_DELAY_MINUTES="0"
IGNORE_SIMULATION="false"

[WAKE_HOST_2]
NAME="Pi"
IP="192.0.2.12"
MAC="DC:A6:32:00:00:12"
AUTO_WOL="true"
IGNORE_SIMULATION="false"
'''


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    conf = tmp_path / 'power_manager.conf'
    conf.write_text(API_CONFIG)
    ups_conf = tmp_path / 'ups.conf'
    ups_conf.write_text('[ups]\n    driver = dummy-ups\n    port = virtual.device\n')
    out = 'battery.charge: 100\nups.status: OL\ndevice.type: ups\n'
    monkeypatch.setattr(api.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, out, ''))
    monkeypatch.setattr(api, 'POWER_MANAGER_CONFIG', str(conf))
    monkeypatch.setattr(api, 'UPS_CONF_FILE', str(ups_conf))
    monkeypatch.setattr(api, 'CLIENT_STATUS_FILE', str(tmp_path / 'client_status.json'))
    monkeypatch.setattr(api, 'POWER_STATE_FILE', str(tmp_path / 'power_state.json'))
    monkeypatch.setattr(api, 'API_TOKEN', TOKEN)
    monkeypatch.setenv('UPS_SERVER_HOST_IP', '192.0.2.1')
    c = api.app.test_client()
    c.tmp = tmp_path
    return c


# --- what the server tells each machine ------------------------------------------

def test_a_keep_running_machine_is_told_not_to_shut_down(api_client):
    data = api_client.get('/config?ip=192.0.2.12', headers=AUTH).get_json()
    assert data['SHUTDOWN_ENABLED'] is False
    # Scripts before 4.5.0 ignore SHUTDOWN_ENABLED and read a missing delay as
    # 0 (shut down now), so they keep getting the old default of 5...
    assert data['SHUTDOWN_DELAY_MINUTES'] == '5'
    # ...and sit out test mode, so rehearsals can't turn them off.
    assert data['IGNORE_SIMULATION'] is True


def test_a_machine_that_turns_off_is_told_as_before(api_client):
    data = api_client.get('/config?ip=192.0.2.10', headers=AUTH).get_json()
    assert data['SHUTDOWN_ENABLED'] is True
    assert data['SHUTDOWN_DELAY_MINUTES'] == '0'
    assert data['IGNORE_SIMULATION'] is False


def test_check_ins_record_the_script_version(api_client):
    api_client.post('/status', headers=AUTH, json={'ip': '192.0.2.12', 'status': 'on_battery',
                                                   'client_version': '4.5.0'})
    api_client.post('/status', headers=AUTH, json={'ip': '192.0.2.10', 'status': 'online',
                                                   'client_version': '<b>nope</b>'})
    saved = json.loads((api_client.tmp / 'client_status.json').read_text())
    assert saved['192.0.2.12']['status'] == 'on_battery'
    assert saved['192.0.2.12']['client_version'] == '4.5.0'
    assert 'client_version' not in saved['192.0.2.10']


# --- telling scripts and boards apart ---------------------------------------------

def test_script_versions():
    assert machine_roles.parse_version('4.5.0') == (4, 5, 0)
    assert machine_roles.parse_version('4.10') == (4, 10, 0)
    assert machine_roles.parse_version('new') is None
    assert machine_roles.understands_keep_running({'status': 'online', 'client_version': '4.5.0'})
    assert machine_roles.understands_keep_running({'status': 'online', 'client_version': '5.0.1'})
    assert not machine_roles.understands_keep_running({'status': 'online', 'client_version': '4.4.9'})
    assert not machine_roles.understands_keep_running({'status': 'online'})


def test_old_script_only_matters_for_keep_running_machines():
    old = {'status': 'online'}
    new = {'status': 'online', 'client_version': '4.5.0'}
    keep = {'IP': '192.0.2.12'}
    off = {'IP': '192.0.2.10', 'SHUTDOWN_DELAY_MINUTES': '0'}
    assert machine_roles.old_script(keep, old)
    assert not machine_roles.old_script(keep, new)
    assert not machine_roles.old_script(off, old)
    # A machine that never ran the script has nothing to update.
    assert not machine_roles.old_script(keep, None)


def test_raspberry_pis_are_recognised():
    prefixes = machine_roles.no_wake_prefixes()
    assert 'DCA632' in prefixes and 'B827EB' in prefixes
    assert machine_roles.no_wake_maker('dc:a6:32:00:00:30', prefixes)
    assert not machine_roles.no_wake_maker('02:00:00:00:00:10', prefixes)
    assert not machine_roles.no_wake_maker('', prefixes)


# --- the pop-up's answers ---------------------------------------------------------

def choices(**fields):
    return web_gui.read_machine_choices(MultiDict(fields))


def test_keep_running_saves_no_delay_and_sits_out_test_mode():
    assert choices(power_cut='keep', when_back='wake', shutdown_delay='7') == (None, 'true', 'true', None)


def test_turn_off_answers():
    # Delays come back in seconds; the unit defaults to minutes.
    assert choices(power_cut='shutdown', shutdown_delay='7', join_test_mode='true',
                   when_back='leave') == (420, 'false', 'false', None)
    assert choices(power_cut='shutdown', shutdown_delay='', when_back='wake') == (0, 'true', 'true', None)
    assert choices(power_cut='shutdown', shutdown_delay='soon')[3]


def test_older_forms_still_work():
    assert choices(shutdown_delay='3', auto_wol='true') == (180, 'false', 'true', None)
    assert choices() == (None, 'false', 'false', None)


def test_saving_keep_running_from_the_pop_up(env):
    client = signed_in(env)
    tok = token(client)
    client.post('/edit_wake_host/WAKE_HOST_1', data={
        'csrf_token': tok, 'name': 'Proxmox', 'ip': '192.0.2.10', 'mac': '02:00:00:00:00:10',
        'power_cut': 'keep', 'when_back': 'leave'})
    host = parsed(env)[1]['WAKE_HOST_1']
    assert 'SHUTDOWN_DELAY_MINUTES' not in host
    assert host['AUTO_WOL'] == 'false' and host['IGNORE_SIMULATION'] == 'true'
    # Battery rules aren't touched.
    assert host['POWER_SOURCE'] == 'battery'


def test_adding_a_machine_that_turns_off(env):
    client = signed_in(env)
    tok = token(client)
    client.post('/add_wake_host', data={
        'csrf_token': tok, 'name': 'NAS', 'ip': '192.0.2.20', 'mac': '02:00:00:00:00:20',
        'power_cut': 'shutdown', 'shutdown_delay': '4', 'join_test_mode': 'true', 'when_back': 'wake'})
    host = parsed(env)[1]['WAKE_HOST_2']
    assert host['SHUTDOWN_DELAY_MINUTES'] == '4'
    assert host['IGNORE_SIMULATION'] == 'false' and host['AUTO_WOL'] == 'true'


def test_settings_page_warns_about_old_scripts_and_pis(env, tmp_path, monkeypatch):
    statuses = tmp_path / 'client_status.json'
    statuses.write_text(json.dumps({'192.0.2.30': {'status': 'online', 'timestamp': '2026-01-01T00:00:00Z'}}))
    monkeypatch.setattr(web_gui, 'CLIENT_STATUS_FILE', str(statuses))
    with open(env['conf'], 'a') as f:
        f.write('\n[WAKE_HOST_2]\nNAME="Pi NAS"\nIP="192.0.2.30"\nMAC="DC:A6:32:00:00:30"\nAUTO_WOL="true"\n')
    html = signed_in(env).get('/config').get_data(as_text=True)
    assert 'Keeps running' in html
    assert 'Older UPS script · still shuts down after 5 min' in html
    assert 'WoL support unsure' in html
    assert 'Starts shutting down immediately' in html


def test_settings_use_shadcn_style_dropdowns(env):
    html = signed_in(env).get('/config').get_data(as_text=True)
    assert '<select' not in html
    # The saved (or default) choice is shown and sent with the form.
    assert re.search(r'id="log_retention_days".*?<input type="hidden" value="7">', html, re.S)
    assert re.search(r'id="smtp_use_tls".*?<input type="hidden" name="smtp_use_tls" value="auto">', html, re.S)
    assert 'for="host-power-cut-trigger"' in html and 'name="when_back"' in html


# --- the dashboard ----------------------------------------------------------------

def status_of(record, old_script=False, reachable=True, power_cut=False):
    return dashboard_view.machine_status('wol', reachable, record, 10, 300, power_cut,
                                         old_script=old_script)['key']


def test_dashboard_shows_staying_on_and_old_scripts():
    assert status_of({'status': 'on_battery'}, power_cut=True) == 'on_battery'
    assert status_of({'status': 'online'}, old_script=True) == 'old_script'
    assert 'old_script' in dashboard_view.ATTENTION_KEYS
    assert status_of({'status': 'online', 'client_version': '4.5.0'}) == 'online'
    # During a power cut the old script's countdown still shows as one.
    assert status_of({'status': 'shutdown_pending'}, old_script=True, power_cut=True) == 'countdown'


# --- stale emails -----------------------------------------------------------------

def stale(tmp_path, monkeypatch, record, power_state):
    path = tmp_path / 'client_status.json'
    ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime('%Y-%m-%dT%H:%M:%SZ')
    path.write_text(json.dumps({'192.0.2.12': dict(record, timestamp=ts)}))
    monkeypatch.setattr(power_manager, 'CLIENT_STATUS_FILE', str(path))
    pm = power_manager.PowerManager.__new__(power_manager.PowerManager)
    pm.config = {'CLIENT_STALE_TIMEOUT_MINUTES': '5', 'WOL_CONFIRM_TIMEOUT_MINUTES': '10'}
    pm.wake_hosts = {'WAKE_HOST_2': {'NAME': 'Pi', 'IP': '192.0.2.12'}}
    pm.power_state = power_state
    pm.client_notification_states = {}
    pm.notifier = Recorder()
    pm._check_client_statuses()
    return pm.notifier.sent


def test_a_silent_keep_running_machine_is_reported_even_in_a_power_cut(tmp_path, monkeypatch):
    sent = stale(tmp_path, monkeypatch, {'status': 'on_battery', 'client_version': '4.5.0'}, 'POWER_FAIL')
    assert 'CLIENT_STALE' in sent


def test_keep_running_machines_with_old_scripts_are_not_watched(tmp_path, monkeypatch):
    # They turn themselves off in a power cut, so going quiet is expected.
    assert stale(tmp_path, monkeypatch, {'status': 'online'}, 'POWER_OK') == []
