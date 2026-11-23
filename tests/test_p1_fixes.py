"""P1 follow-up fixes: quiet stale alerts after a switch-off, two silent checks
before a power cut, and log rotation rules that logrotate will actually read."""

import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'app'))

import dashboard_view  # noqa: E402
import power_manager  # noqa: E402
from test_small_fixes import stale_check  # noqa: E402


# --- stale alerts -------------------------------------------------------------------------

def test_no_stale_email_after_the_script_reported_switching_off(tmp_path, monkeypatch):
    # The last thing the script sends before switching off, with or without a
    # countdown. Power is back and the machine is waiting for Wake-on-LAN.
    sent = stale_check(tmp_path, monkeypatch, 'shutting_down', 30, power_state='POWER_RESTORED')
    assert 'CLIENT_STALE' not in sent


# --- two silent checks before a power cut --------------------------------------------------

SENTINELS = ['192.0.2.101', '192.0.2.102']


def fresh_manager(tmp_path, monkeypatch, answering, simulation=False, real_fail_notified=False):
    """A new PowerManager per check, as each cron iteration is a new process."""
    monkeypatch.setattr(power_manager, 'SENTINEL_SILENCE_STATE_FILE',
                        str(tmp_path / 'sentinel_silence.json'))
    monkeypatch.setattr(power_manager.reachability, 'check',
                        lambda ips, **kw: {ip: ip in answering for ip in ips})
    saved = []
    monkeypatch.setattr(power_manager, 'save_setting_to_config',
                        lambda key, value, **kw: saved.append((key, value)))
    pm = power_manager.PowerManager.__new__(power_manager.PowerManager)
    pm.config = {'SENTINEL_HOSTS': ' '.join(SENTINELS),
                 'POWER_SIMULATION_MODE': 'true' if simulation else 'false'}
    pm.sentinel_results = {}
    pm.sentinel_confirming = False
    pm.real_fail_notified = real_fail_notified
    pm.simulation_interrupted = False
    pm.saved = saved
    pm._should_simulation_be_active_now = lambda: {'active': False}
    return pm


def check(tmp_path, monkeypatch, answering=(), **kw):
    pm = fresh_manager(tmp_path, monkeypatch, set(answering), **kw)
    return pm, pm._determine_power_status()


def test_one_silent_check_is_not_a_power_cut(tmp_path, monkeypatch):
    pm, status = check(tmp_path, monkeypatch)
    assert status == 'ONLINE'
    assert pm.real_power_offline is False
    assert pm.sentinel_confirming is True


def test_two_silent_checks_in_a_row_are_a_power_cut(tmp_path, monkeypatch):
    check(tmp_path, monkeypatch)
    pm, status = check(tmp_path, monkeypatch)
    assert status == 'OFFLINE'
    assert pm.real_power_offline is True
    assert pm.sentinel_confirming is False
    # And it stays a power cut while the silence lasts.
    pm, status = check(tmp_path, monkeypatch)
    assert status == 'OFFLINE'


def test_a_sentinel_answering_in_between_starts_the_count_again(tmp_path, monkeypatch):
    check(tmp_path, monkeypatch)
    pm, status = check(tmp_path, monkeypatch, answering=[SENTINELS[1]])
    assert status == 'ONLINE' and pm.sentinel_confirming is False
    pm, status = check(tmp_path, monkeypatch)
    assert status == 'ONLINE' and pm.sentinel_confirming is True


def test_power_returns_on_the_first_answer(tmp_path, monkeypatch):
    check(tmp_path, monkeypatch)
    check(tmp_path, monkeypatch)
    pm, status = check(tmp_path, monkeypatch, answering=[SENTINELS[0]])
    assert status == 'ONLINE' and pm.real_power_offline is False


def test_an_announced_outage_is_not_undone_by_a_missing_counter(tmp_path, monkeypatch):
    # E.g. the first check after updating mid-outage: the counter file doesn't
    # exist yet, but the outage email has already gone out.
    pm, status = check(tmp_path, monkeypatch, real_fail_notified=True)
    assert status == 'OFFLINE'
    assert pm.real_power_offline is True


def test_if_the_count_cannot_be_saved_silence_counts_at_once(tmp_path, monkeypatch):
    pm = fresh_manager(tmp_path, monkeypatch, set())
    monkeypatch.setattr(power_manager, 'SENTINEL_SILENCE_STATE_FILE',
                        str(tmp_path / 'missing-folder' / 'sentinel_silence.json'))
    assert pm._determine_power_status() == 'OFFLINE'


def test_test_mode_is_not_interrupted_by_one_silent_check(tmp_path, monkeypatch):
    pm, status = check(tmp_path, monkeypatch, simulation=True)
    assert status == 'OFFLINE'            # test mode still forces it
    assert pm.real_power_offline is False
    assert pm.saved == []                 # test mode left on
    pm, status = check(tmp_path, monkeypatch, simulation=True)
    assert pm.real_power_offline is True
    assert ('POWER_SIMULATION_MODE', 'false') in pm.saved


def test_dashboard_says_it_is_checking_again():
    state = {'sentinel': {'online': 0, 'total': 2, 'power': 'ONLINE', 'confirming': True}}
    banner = dashboard_view.build_banner(state, False, {}, [], 0, 2, 1, '', None)
    assert banner['title'] == 'No sentinel is answering — checking again'
    assert banner['tone'] == 'warning'


def test_dashboard_shows_a_confirmed_power_cut():
    state = {'sentinel': {'online': 0, 'total': 2, 'power': 'OFFLINE', 'confirming': False}}
    banner = dashboard_view.build_banner(state, False, {}, [], 0, 2, 1, '', None)
    assert banner['title'] == 'Power cut — shutting machines down'


# --- 10-second checks ---------------------------------------------------------------------

def test_six_checks_of_ten_seconds_fill_the_minute():
    assert power_manager.CHECK_ITERATIONS == 6
    assert power_manager.CHECK_INTERVAL_SECONDS == 10


def test_silent_sentinels_are_pinged_at_once(tmp_path, monkeypatch):
    # Five sentinels that never answer, with a ping that takes its full
    # one-second timeout: one after another this was five seconds.
    script = tmp_path / 'slow-ping'
    script.write_text('#!/bin/sh\nsleep 1\nexit 1\n')
    script.chmod(0o755)
    monkeypatch.setattr(power_manager, 'PING_CMD', str(script))
    monkeypatch.setattr(power_manager, 'SENTINEL_SILENCE_STATE_FILE',
                        str(tmp_path / 'sentinel_silence.json'))
    pm = power_manager.PowerManager.__new__(power_manager.PowerManager)
    pm.config = {'SENTINEL_HOSTS': ' '.join(f'192.0.2.{n}' for n in range(1, 6))}
    pm.sentinel_results = {}
    pm.real_fail_notified = False
    started = time.monotonic()
    pm._determine_power_status()
    assert time.monotonic() - started < 2.5
    assert pm.sentinel_results == {f'192.0.2.{n}': False for n in range(1, 6)}


def battery_manager(**config):
    pm = power_manager.PowerManager.__new__(power_manager.PowerManager)
    pm.config = config
    return pm


def test_battery_count_settings_keep_their_real_world_length():
    # Written for 15s checks: 4 was a minute, 8 was two minutes.
    assert battery_manager()._fallback_grace_cycles() == 6
    assert battery_manager()._battery_only_outage_cycles() == 12
    assert battery_manager(BATTERY_FALLBACK_GRACE_CYCLES='4')._fallback_grace_cycles() == 6
    assert battery_manager(BATTERY_ONLY_OUTAGE_CYCLES='8')._battery_only_outage_cycles() == 12


def test_battery_count_settings_keep_zero_and_one():
    assert battery_manager(BATTERY_FALLBACK_GRACE_CYCLES='0')._fallback_grace_cycles() == 0
    assert battery_manager(BATTERY_ONLY_OUTAGE_CYCLES='1')._battery_only_outage_cycles() == 1


# --- log rotation -------------------------------------------------------------------------

def read(name):
    with open(os.path.join(ROOT, name)) as f:
        return f.read()


def test_rotation_rules_come_from_the_image_not_a_mount():
    # logrotate skips rule files not owned by root, and a file mounted from the
    # host keeps the host owner, so power_manager.log was never rotated.
    compose = read('docker-compose.yml.example')
    mounts = [line for line in compose.splitlines()
              if re.match(r'\s*-\s*\./logrotate/', line)]
    assert mounts == []
    assert 'COPY logrotate/power-manager-logrotate /etc/logrotate.d/' in read('Dockerfile')
