"""The dashboard's wording and status rules, without Flask or files."""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app'))

from dashboard_view import build_dashboard, machine_status  # noqa: E402

NOW = datetime(2026, 11, 19, 10, 0, 0, tzinfo=timezone.utc)
NOW_LOCAL = datetime(2026, 11, 19, 15, 30, 0)

CONFIG = {
    'SENTINEL_HOSTS': '192.0.2.101 192.0.2.102',
    'CLIENT_STALE_TIMEOUT_MINUTES': '2',
    'WOL_DELAY_MINUTES': '3',
    'POWER_SIMULATION_MODE': 'false',
}
HOSTS = {
    'WAKE_HOST_1': {'NAME': 'Proxmox', 'IP': '192.0.2.10', 'MAC': '02:00:00:00:00:10',
                    'SHUTDOWN_DELAY_MINUTES': '0'},
    'WAKE_HOST_2': {'NAME': 'NAS', 'IP': '192.0.2.11', 'MAC': '02:00:00:00:00:11',
                    'SHUTDOWN_DELAY_MINUTES': '2', 'IGNORE_SIMULATION': 'true'},
    'WAKE_HOST_3': {'NAME': 'Media PC', 'IP': '192.0.2.13', 'MAC': '02:00:00:00:00:13'},
}


def stamp(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')


def state(checked_ago=5):
    return {'updated_at': stamp(checked_ago), 'battery': {'enabled': False},
            'reachability': {'checked_at': stamp(checked_ago)}, 'hosts': {}}


def view(config=None, sentinels=None, hosts=None, clients=None, manager=None, power_state='default'):
    return build_dashboard(
        dict(CONFIG, **(config or {})), HOSTS, {}, state() if power_state == 'default' else power_state,
        clients or {}, manager or {},
        sentinels if sentinels is not None else {'192.0.2.101': True, '192.0.2.102': True},
        hosts if hosts is not None else {'192.0.2.10': True, '192.0.2.11': True, '192.0.2.13': True},
        now=NOW, now_local=NOW_LOCAL)


def online(seconds_ago=5):
    return {'status': 'online', 'timestamp': stamp(seconds_ago)}


def test_all_good():
    v = view(clients={'192.0.2.10': online(), '192.0.2.11': online()})
    assert v['banner']['tone'] == 'success'
    assert v['banner']['title'] == 'Mains power is on'
    assert 'All 2 sentinels are answering' in v['banner']['text']
    assert not v['watch_stale']
    assert v['clients'][0]['agent_text'] == 'Reporting'
    assert [m['status']['label'] for m in v['clients']] == ['Online', 'Online']
    assert v['others'][0]['status']['label'] == 'Online'


def test_machine_without_agent_needs_attention():
    v = view(clients={'192.0.2.10': online()})
    nas = v['clients'][1]
    assert nas['status']['label'] == 'No agent'
    assert nas['needs_attention']
    assert '1 machine needs attention' in v['banner']['text']


def test_stale_agent():
    v = view(clients={'192.0.2.10': online(), '192.0.2.11': online(seconds_ago=600)})
    assert v['clients'][1]['status']['label'] == 'Status stale'
    assert v['clients'][1]['agent_text'] == 'Last heard 10 min ago'


def test_power_cut_with_countdown():
    clients = {
        '192.0.2.10': {'status': 'shutting_down', 'timestamp': stamp(60)},
        '192.0.2.11': {'status': 'shutdown_pending', 'timestamp': stamp(10),
                       'remaining_seconds': 100, 'shutdown_delay': 2},
    }
    manager = {'STATE': 'POWER_FAIL', 'TIMESTAMP': str(int(NOW_LOCAL.timestamp()) - 120)}
    v = view(sentinels={'192.0.2.101': False, '192.0.2.102': False},
             hosts={'192.0.2.10': False, '192.0.2.11': True, '192.0.2.13': False},
             clients=clients, manager=manager)
    assert v['banner']['tone'] == 'danger'
    assert v['banner']['title'] == 'Power cut — shutting machines down'
    proxmox, nas = v['clients']
    assert proxmox['status']['label'] == 'Offline'
    assert proxmox['status']['tone'] == 'muted'
    assert proxmox['status']['note'] == 'Turned off safely for the power cut.'
    # Problem hints carry a tone so they stay visible.
    assert v['tiles'][0]['hint'] == '2 not answering'
    assert v['tiles'][0]['hint_tone'] == 'danger'
    assert nas['status']['label'] == 'Shutting down'
    assert nas['countdown']['remaining'] == 90
    assert not any(m['needs_attention'] for m in v['clients'])
    # Waking during a cut would only drain the UPS.
    assert not proxmox['can_wake']


def test_power_back_waiting_to_wake():
    manager = {'STATE': 'POWER_RESTORED', 'TIMESTAMP': str(int(NOW_LOCAL.timestamp()) - 60)}
    v = view(hosts={'192.0.2.10': False, '192.0.2.11': False, '192.0.2.13': False},
             clients={'192.0.2.10': {'status': 'shutting_down', 'timestamp': stamp(900)}},
             manager=manager)
    assert v['banner']['tone'] == 'info'
    assert v['banner']['title'].startswith('Power is back — waking machines in')
    assert v['clients'][0]['status']['label'] == 'Waiting to wake'
    assert v['clients'][0]['can_wake']


def test_test_mode_counts_machines_that_act():
    v = view(config={'POWER_SIMULATION_MODE': 'true'},
             clients={'192.0.2.10': online(), '192.0.2.11': online()})
    assert v['banner']['title'] == 'Test mode is on'
    assert '1 machine without' in v['banner']['text']


def test_no_snapshot_means_unknown():
    v = view(power_state=None)
    assert v['banner']['title'] == "Can't confirm the power state"


def test_did_not_wake_up():
    status = machine_status('wol', False, {'status': 'wol_no_response'}, 30, 120, False)
    assert status['label'] == 'Did not wake up'
    assert status['tone'] == 'danger'


def test_switched_off_media_pc_is_not_a_problem():
    v = view(hosts={'192.0.2.10': True, '192.0.2.11': True, '192.0.2.13': False},
             clients={'192.0.2.10': online(), '192.0.2.11': online()})
    media = v['others'][0]
    assert media['status']['label'] == 'Offline'
    assert not media['needs_attention']
    assert v['banner']['tone'] == 'success'


def test_quiet_watchman_is_flagged():
    v = view(power_state=state(checked_ago=300))
    assert v['watch_stale']


def test_agent_never_reported():
    v = view(clients={'192.0.2.10': online()})
    assert v['clients'][1]['agent_text'] == 'Never reported'


def test_unexpected_offline_needs_attention():
    v = view(hosts={'192.0.2.10': False, '192.0.2.11': True, '192.0.2.13': True},
             clients={'192.0.2.11': online()})
    proxmox = v['clients'][0]
    assert proxmox['status']['label'] == 'Offline'
    assert proxmox['status']['tone'] == 'danger'
    assert proxmox['needs_attention']
    assert v['tiles'][1]['hint_tone'] == 'danger'


def test_quiet_hints_have_no_tone():
    v = view(clients={'192.0.2.10': online(), '192.0.2.11': online()})
    assert all(not t.get('hint_tone') for t in v['tiles'])
