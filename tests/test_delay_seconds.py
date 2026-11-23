"""Shutdown delays in seconds or minutes, an explicit "immediately", and what
scripts too old for seconds are sent."""

import json
import os
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
import web_gui  # noqa: E402
from test_web_settings import env, parsed, signed_in, token  # noqa: E402,F401


# --- reading and writing the delay ---------------------------------------------------

def test_delay_in_seconds_wins_over_minutes():
    assert machine_roles.delay_seconds({'SHUTDOWN_DELAY_MINUTES': '1', 'SHUTDOWN_DELAY_SECONDS': '30'}) == 30
    assert machine_roles.delay_seconds({'SHUTDOWN_DELAY_MINUTES': '3'}) == 180
    assert machine_roles.delay_seconds({'SHUTDOWN_DELAY_MINUTES': '0'}) == 0
    assert machine_roles.delay_seconds({}) is None                    # keeps running
    assert machine_roles.delay_seconds({'SHUTDOWN_DELAY_MINUTES': 'abc'}) is None


def test_old_scripts_get_whole_minutes_rounded_up():
    assert machine_roles.minutes_for_old_scripts(0) == '0'
    assert machine_roles.minutes_for_old_scripts(30) == '1'
    assert machine_roles.minutes_for_old_scripts(60) == '1'
    assert machine_roles.minutes_for_old_scripts(90) == '2'


def test_seconds_are_only_written_when_needed():
    assert machine_roles.delay_settings(30) == {'SHUTDOWN_DELAY_MINUTES': '1', 'SHUTDOWN_DELAY_SECONDS': '30'}
    assert machine_roles.delay_settings(120) == {'SHUTDOWN_DELAY_MINUTES': '2', 'SHUTDOWN_DELAY_SECONDS': None}
    assert machine_roles.delay_settings(0) == {'SHUTDOWN_DELAY_MINUTES': '0', 'SHUTDOWN_DELAY_SECONDS': None}


def test_delay_wording():
    assert machine_roles.delay_text(30) == '30 s'
    assert machine_roles.delay_text(120) == '2 min'
    assert machine_roles.delay_text(90) == '1 min 30 s'


def test_apply_delay_keeps_the_two_settings_in_step():
    host = {'SHUTDOWN_DELAY_MINUTES': '1', 'SHUTDOWN_DELAY_SECONDS': '45'}
    web_gui.apply_delay(host, 120)
    assert host == {'SHUTDOWN_DELAY_MINUTES': '2'}
    web_gui.apply_delay(host, 30)
    assert host == {'SHUTDOWN_DELAY_MINUTES': '1', 'SHUTDOWN_DELAY_SECONDS': '30'}
    web_gui.apply_delay(host, None)
    assert host == {}


# --- the pop-up's answers ------------------------------------------------------------

def choices(**fields):
    return web_gui.read_machine_choices(MultiDict(fields))


def test_immediately_ignores_the_delay_field():
    assert choices(power_cut='shutdown', start='now', shutdown_delay='5', delay_unit='minutes')[0] == 0


def test_after_a_delay_in_seconds_or_minutes():
    assert choices(power_cut='shutdown', start='after', shutdown_delay='30', delay_unit='seconds')[0] == 30
    assert choices(power_cut='shutdown', start='after', shutdown_delay='2', delay_unit='minutes')[0] == 120


def test_delays_over_a_day_or_not_whole_are_refused():
    assert choices(power_cut='shutdown', start='after', shutdown_delay='1441', delay_unit='minutes')[3]
    assert choices(power_cut='shutdown', start='after', shutdown_delay='86401', delay_unit='seconds')[3]
    assert choices(power_cut='shutdown', start='after', shutdown_delay='2.5', delay_unit='minutes')[3]


def test_saving_thirty_seconds_from_the_pop_up(env):
    client = signed_in(env)
    client.post('/edit_wake_host/WAKE_HOST_1', data={
        'csrf_token': token(client), 'name': 'Proxmox', 'ip': '192.0.2.10', 'mac': '02:00:00:00:00:10',
        'power_cut': 'shutdown', 'start': 'after', 'shutdown_delay': '30', 'delay_unit': 'seconds',
        'when_back': 'wake'})
    host = parsed(env)[1]['WAKE_HOST_1']
    assert host['SHUTDOWN_DELAY_MINUTES'] == '1'
    assert host['SHUTDOWN_DELAY_SECONDS'] == '30'
    html = client.get('/config').get_data(as_text=True)
    assert 'Starts shutting down after 30 s' in html


def test_settings_page_has_the_new_choices(env):
    html = signed_in(env).get('/config').get_data(as_text=True)
    assert '>Shut down<' in html and 'Turn itself off' not in html
    assert '<legend>Start shutting down</legend>' in html
    assert 'name="start" value="now"' in html and 'name="start" value="after"' in html
    assert 'name="delay_unit"' in html


# --- what the scripts are sent ---------------------------------------------------------

TOKEN = 'delay-seconds-token'
AUTH = {'Authorization': f'Bearer {TOKEN}'}
API_CONFIG = f'''API_TOKEN="{TOKEN}"
POWER_SIMULATION_MODE="false"

[WAKE_HOST_1]
NAME="Proxmox"
IP="192.0.2.10"
MAC="02:00:00:00:00:10"
SHUTDOWN_DELAY_MINUTES="1"
SHUTDOWN_DELAY_SECONDS="30"

[WAKE_HOST_2]
NAME="NAS"
IP="192.0.2.11"
MAC="02:00:00:00:00:11"
SHUTDOWN_DELAY_MINUTES="3"
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


def test_config_sends_seconds_and_whole_minutes(api_client):
    data = api_client.get('/config?ip=192.0.2.10', headers=AUTH).get_json()
    assert data['SHUTDOWN_DELAY_SECONDS'] == '30'
    assert data['SHUTDOWN_DELAY_MINUTES'] == '1'      # what older scripts read
    data = api_client.get('/config?ip=192.0.2.11', headers=AUTH).get_json()
    assert data['SHUTDOWN_DELAY_SECONDS'] == '180'
    assert data['SHUTDOWN_DELAY_MINUTES'] == '3'


def test_status_keeps_the_reported_seconds(api_client):
    api_client.post('/status', headers=AUTH, json={
        'ip': '192.0.2.10', 'status': 'shutdown_pending', 'remaining_seconds': 25,
        'shutdown_delay': 1, 'shutdown_delay_seconds': 30, 'client_version': '4.6.0'})
    record = json.loads((api_client.tmp / 'client_status.json').read_text())['192.0.2.10']
    assert record['shutdown_delay_seconds'] == 30
    assert record['shutdown_delay'] == 1


# --- warnings and the dashboard ----------------------------------------------------------

def notes_for(version, params):
    record = {'status': 'online', 'client_version': version} if version else {'status': 'online'}
    return machine_roles.machine_notes({'WAKE_HOST_1': dict(params, IP='192.0.2.10')},
                                       {'192.0.2.10': record}, [])['WAKE_HOST_1']


def test_older_scripts_are_flagged_when_they_round_up():
    thirty = {'SHUTDOWN_DELAY_MINUTES': '1', 'SHUTDOWN_DELAY_SECONDS': '30'}
    assert notes_for('4.5.0', thirty)['rounded_delay'] == '1 min'
    assert notes_for(None, thirty)['rounded_delay'] == '1 min'          # older than 4.5.0
    assert notes_for('4.6.0', thirty)['rounded_delay'] is None
    assert notes_for('4.5.0', {'SHUTDOWN_DELAY_MINUTES': '2'})['rounded_delay'] is None


def test_card_text_for_each_delay():
    assert notes_for('4.6.0', {'SHUTDOWN_DELAY_MINUTES': '0'})['delay'] == 'immediately'
    assert notes_for('4.6.0', {'SHUTDOWN_DELAY_MINUTES': '1', 'SHUTDOWN_DELAY_SECONDS': '30'})['delay'] == 'after 30 s'
    assert notes_for('4.6.0', {})['delay'] is None


NOW = datetime(2026, 11, 23, 10, 0, 0, tzinfo=timezone.utc)


def test_dashboard_countdown_uses_the_reported_seconds():
    stamp = (NOW - timedelta(seconds=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
    view = dashboard_view.build_dashboard(
        {'SENTINEL_HOSTS': '192.0.2.101', 'CLIENT_STALE_TIMEOUT_MINUTES': '2', 'WOL_DELAY_MINUTES': '3'},
        {'WAKE_HOST_1': {'NAME': 'Proxmox', 'IP': '192.0.2.10', 'MAC': '02:00:00:00:00:10',
                         'SHUTDOWN_DELAY_MINUTES': '1', 'SHUTDOWN_DELAY_SECONDS': '30'}},
        {},
        {'updated_at': stamp, 'sentinel': {'online': 0, 'total': 1, 'power': 'OFFLINE'}},
        {'192.0.2.10': {'status': 'shutdown_pending', 'remaining_seconds': 25, 'shutdown_delay': 1,
                        'shutdown_delay_seconds': 30, 'timestamp': stamp}},
        {'STATE': 'POWER_FAIL'}, {'192.0.2.101': False}, {'192.0.2.10': True},
        now=NOW, now_local=NOW.replace(tzinfo=None))
    machine = view['clients'][0]
    assert machine['delay_text'] == 'After 30 s'
    assert machine['countdown']['total'] == 30
