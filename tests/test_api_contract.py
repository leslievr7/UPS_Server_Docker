"""The answers the UPS_monitor clients rely on.

ups_monitor.sh compares .ups.status to the exact string "OB LB", reads
.ups.simulation, reads SHUTDOWN_DELAY_MINUTES / IGNORE_SIMULATION / UPS_NAME
from /config, and posts heartbeats to /status. If any of these tests fail,
the client machines would misread the server - fix the server, not the test.

upsc is replaced by canned output, so no command is run.
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app'))

import api  # noqa: E402

TOKEN = 'contract-test-token'
AUTH = {'Authorization': f'Bearer {TOKEN}'}

CONFIG = '''API_TOKEN="{token}"
POWER_SIMULATION_MODE="{sim}"

[WAKE_HOST_1]
NAME="Proxmox"
IP="192.0.2.10"
MAC="02:00:00:00:00:10"
SHUTDOWN_DELAY_MINUTES="0"
IGNORE_SIMULATION="false"

[WAKE_HOST_2]
NAME="Pi"
IP="192.0.2.12"
MAC="02:00:00:00:00:12"
SHUTDOWN_DELAY_MINUTES="3"
IGNORE_SIMULATION="true"
'''


@pytest.fixture
def client(tmp_path, monkeypatch):
    conf = tmp_path / 'power_manager.conf'
    ups_conf = tmp_path / 'ups.conf'
    ups_conf.write_text('[ups]\n    driver = dummy-ups\n    port = virtual.device\n')

    def configure(sim='false', status='OL'):
        conf.write_text(CONFIG.format(token=TOKEN, sim=sim))
        out = f'battery.charge: 100\nups.status: {status}\ndevice.type: ups\n'
        monkeypatch.setattr(api.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, out, ''))

    monkeypatch.setattr(api, 'POWER_MANAGER_CONFIG', str(conf))
    monkeypatch.setattr(api, 'UPS_CONF_FILE', str(ups_conf))
    monkeypatch.setattr(api, 'CLIENT_STATUS_FILE', str(tmp_path / 'client_status.json'))
    monkeypatch.setattr(api, 'POWER_STATE_FILE', str(tmp_path / 'power_state.json'))
    monkeypatch.setattr(api, 'API_TOKEN', TOKEN)
    # Set from .env on the real server.
    monkeypatch.setenv('UPS_SERVER_HOST_IP', '192.0.2.1')
    configure()
    c = api.app.test_client()
    c.configure = configure
    c.tmp = tmp_path
    return c


def test_every_client_endpoint_needs_the_token(client):
    assert client.get('/upsc').status_code == 401
    assert client.get('/config?ip=192.0.2.10').status_code == 401
    assert client.post('/status', json={'ip': '192.0.2.10', 'status': 'online'}).status_code == 401


def test_upsc_mains_ok(client):
    data = client.get('/upsc', headers=AUTH).get_json()
    assert data['ups']['status'] == 'OL'
    assert data['ups']['simulation'] is False


def test_upsc_on_battery_low_is_exactly_OB_LB(client):
    client.configure(status='OB LB')
    data = client.get('/upsc', headers=AUTH).get_json()
    assert data['ups']['status'] == 'OB LB'


def test_upsc_reports_test_mode(client):
    client.configure(sim='true', status='OB LB')
    data = client.get('/upsc', headers=AUTH).get_json()
    assert data['ups']['simulation'] is True
    assert data['ups']['status'] == 'OB LB'


def test_config_for_a_client(client):
    data = client.get('/config?ip=192.0.2.12', headers=AUTH).get_json()
    assert data['SHUTDOWN_DELAY_MINUTES'] == '3'
    assert data['IGNORE_SIMULATION'] is True
    assert data['UPS_NAME'] == 'ups@192.0.2.1'


def test_config_immediate_shutdown_stays_zero(client):
    # A delay of 0 means "shut down immediately"; it must not turn into the default of 5.
    data = client.get('/config?ip=192.0.2.10', headers=AUTH).get_json()
    assert data['SHUTDOWN_DELAY_MINUTES'] == '0'
    assert data['IGNORE_SIMULATION'] is False


def test_config_unknown_machine_is_refused(client):
    # Upstream answers 500 here (its own 404 is caught and re-raised); the
    # client only checks for success, so any error is fine.
    assert client.get('/config?ip=192.0.2.99', headers=AUTH).status_code >= 400


def test_status_heartbeat_is_recorded(client):
    r = client.post('/status', headers=AUTH, json={
        'ip': '192.0.2.10', 'status': 'shutdown_pending', 'remaining_seconds': 120, 'shutdown_delay': 2})
    assert r.status_code == 200
    saved = json.loads((client.tmp / 'client_status.json').read_text())['192.0.2.10']
    assert saved['status'] == 'shutdown_pending'
    assert saved['remaining_seconds'] == 120
