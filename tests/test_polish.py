"""Switches that save instantly, schedule pausing, stopping test mode from the
dashboard, and sentinel names on the dashboard."""

import os
import re
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'app'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))

import network_scan  # noqa: E402
import web_gui  # noqa: E402
from test_onboarding import make_env, run_fake_scan, scan_files, signed_in, token  # noqa: E402,F401

PAST = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
FUTURE = (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d')

SCHEDULES = f'''
[SCHEDULE_1]
NAME="Weekly test"
TYPE="recurring"
TIME="03:00"
ACTION="start"
ENABLED="true"
DAY_OF_WEEK="sunday"

[SCHEDULE_2]
NAME="Holiday test"
TYPE="one-time"
TIME="10:00"
ACTION="start"
ENABLED="false"
DATE="{PAST}"
'''

FETCH = {'X-Requested-With': 'fetch'}


def env_with_schedules(tmp_path, monkeypatch):
    conf, client = make_env(tmp_path, monkeypatch, sentinels='192.0.2.101')
    conf.write_text(conf.read_text() + SCHEDULES + 'NOTIFY_POWER_FAIL="true"\n')
    signed_in(client)
    return conf, client, token(client, '/config')


def config():
    return web_gui.read_power_manager_config()


# --- switches that save instantly ----------------------------------------------------

def test_a_notification_switch_saves_on_its_own(tmp_path, monkeypatch):
    _, client, tok = env_with_schedules(tmp_path, monkeypatch)
    r = client.post('/settings/switch', data={'name': 'NOTIFY_CLIENT_STALE', 'state': 'on'},
                    headers=dict(FETCH, **{'X-CSRF-Token': tok}))
    assert r.get_json() == {'success': True, 'on': True, 'message': 'Script stopped checking in emails on.'}
    assert config()[0]['NOTIFY_CLIENT_STALE'] == 'true'


def test_only_listed_settings_can_be_switched(tmp_path, monkeypatch):
    conf, client, tok = env_with_schedules(tmp_path, monkeypatch)
    before = conf.read_text()
    r = client.post('/settings/switch', data={'name': 'POWER_SIMULATION_MODE', 'state': 'on'},
                    headers=dict(FETCH, **{'X-CSRF-Token': tok}))
    assert r.status_code == 400 and conf.read_text() == before
    r = client.post('/settings/switch', data={'name': 'NOTIFY_APP_ERROR', 'state': 'on'}, headers=FETCH)
    assert r.status_code == 400 and conf.read_text() == before  # no form token


def test_saving_email_settings_leaves_the_switches_alone(tmp_path, monkeypatch):
    conf, client, tok = env_with_schedules(tmp_path, monkeypatch)
    client.post('/settings/switch', data={'name': 'NOTIFY_POWER_RESTORED', 'state': 'on'},
                headers=dict(FETCH, **{'X-CSRF-Token': tok}))
    client.post('/settings/notifications', data={'csrf_token': tok, 'smtp_server': 'smtp.example.com'})
    assert config()[0]['NOTIFY_POWER_RESTORED'] == 'true'
    assert config()[0]['SMTP_SERVER'] == 'smtp.example.com'


def test_detailed_logging_is_not_reset_by_saving_general(tmp_path, monkeypatch):
    _, client, tok = env_with_schedules(tmp_path, monkeypatch)
    client.post('/settings/switch', data={'name': 'DEBUG_MODE', 'state': 'on'},
                headers=dict(FETCH, **{'X-CSRF-Token': tok}))
    client.post('/settings/general', data={
        'csrf_token': tok, 'sentinel_hosts': '192.0.2.101', 'wol_delay_minutes': '3',
        'wol_confirm_timeout_minutes': '10', 'client_stale_timeout_minutes': '5',
        'default_broadcast_ip': '192.0.2.255'})
    assert config()[0]['DEBUG_MODE'] == 'true'


def test_settings_page_marks_switches_as_instant(tmp_path, monkeypatch):
    _, client, _ = env_with_schedules(tmp_path, monkeypatch)
    html = client.get('/config').get_data(as_text=True)
    assert 'data-autosave="NOTIFY_POWER_FAIL"' in html and 'data-autosave="DEBUG_MODE"' in html
    assert 'Save email settings' in html


# --- schedules ----------------------------------------------------------------------

def test_pausing_and_resuming_a_schedule_from_the_list(tmp_path, monkeypatch):
    _, client, tok = env_with_schedules(tmp_path, monkeypatch)
    headers = dict(FETCH, **{'X-CSRF-Token': tok})
    r = client.post('/schedule/SCHEDULE_1/enabled', data={'state': 'off'}, headers=headers)
    assert r.get_json()['message'] == 'Weekly test paused.'
    assert config()[2]['SCHEDULE_1']['ENABLED'] == 'false'
    r = client.post('/schedule/SCHEDULE_1/enabled', data={'state': 'on'}, headers=headers)
    assert r.get_json()['message'] == 'Weekly test resumed.'
    assert config()[2]['SCHEDULE_1']['ENABLED'] == 'true'


def test_a_one_time_schedule_that_ran_asks_for_a_new_date(tmp_path, monkeypatch):
    _, client, tok = env_with_schedules(tmp_path, monkeypatch)
    r = client.post('/schedule/SCHEDULE_2/enabled', data={'state': 'on'},
                    headers=dict(FETCH, **{'X-CSRF-Token': tok}))
    assert r.get_json()['needs_date'] is True
    assert config()[2]['SCHEDULE_2']['ENABLED'] == 'false'
    # The pop-up then saves a future date and turns it on.
    form = {'csrf_token': tok, 'name': 'Holiday test', 'type': 'one-time', 'time': '10:00',
            'action': 'start', 'date': FUTURE, 'enable': '1'}
    client.post('/edit_schedule/SCHEDULE_2', data=form)
    s = config()[2]['SCHEDULE_2']
    assert s['ENABLED'] == 'true' and s['DATE'] == FUTURE


def test_turning_a_schedule_on_with_a_past_date_is_refused(tmp_path, monkeypatch):
    _, client, tok = env_with_schedules(tmp_path, monkeypatch)
    form = {'csrf_token': tok, 'name': 'Holiday test', 'type': 'one-time', 'time': '10:00',
            'action': 'start', 'date': PAST, 'enable': '1'}
    client.post('/edit_schedule/SCHEDULE_2', data=form)
    assert config()[2]['SCHEDULE_2']['ENABLED'] == 'false'
    client.post('/add_schedule', data=dict(form, name='Old'))
    assert len(config()[2]) == 2


def test_editing_a_paused_schedule_keeps_it_paused(tmp_path, monkeypatch):
    _, client, tok = env_with_schedules(tmp_path, monkeypatch)
    client.post('/edit_schedule/SCHEDULE_2', data={
        'csrf_token': tok, 'name': 'Holiday rehearsal', 'type': 'one-time', 'time': '10:00',
        'action': 'start', 'date': PAST})
    s = config()[2]['SCHEDULE_2']
    assert s['NAME'] == 'Holiday rehearsal' and s['ENABLED'] == 'false'


def test_schedule_list_has_switches_and_one_time_badge(tmp_path, monkeypatch):
    _, client, _ = env_with_schedules(tmp_path, monkeypatch)
    html = client.get('/config').get_data(as_text=True)
    assert 'data-schedule-toggle="SCHEDULE_1"' in html
    assert re.search(r'data-schedule-toggle="SCHEDULE_1"[^>]*checked', html)
    assert not re.search(r'data-schedule-toggle="SCHEDULE_2"[^>]*checked', html)
    assert 'One-time' in html and 'Already ran' in html
    assert 'id="schedule-enabled"' not in html


# --- test mode banner ------------------------------------------------------------------

def test_stop_test_mode_from_the_dashboard(tmp_path, monkeypatch):
    conf, client, tok = env_with_schedules(tmp_path, monkeypatch)
    conf.write_text(conf.read_text().replace('POWER_SIMULATION_MODE="false"', 'POWER_SIMULATION_MODE="true"'))
    r = client.post('/settings/test_mode', data={'csrf_token': tok, 'state': 'off', 'next': 'dashboard'})
    assert r.headers['Location'] == '/'
    assert config()[0]['POWER_SIMULATION_MODE'] == 'false'


# --- sentinel names ----------------------------------------------------------------------

def test_picked_sentinels_show_by_name_on_the_dashboard(tmp_path, monkeypatch, scan_files):
    _, client = make_env(tmp_path, monkeypatch)
    signed_in(client)
    run_fake_scan()
    tok = token(client, '/sentinels/find?guide=skip')
    client.post('/sentinels/save', data={'csrf_token': tok, 'ip': ['192.0.2.21', '192.0.2.50', '192.0.2.61']})
    names = network_scan.parse_sentinel_names(config()[0]['SENTINEL_NAMES'])
    assert names['192.0.2.21'] == {'title': 'shellyplug-s-kitchen', 'icon': 'plug', 'maker': 'Shelly'}
    assert names['192.0.2.50']['title'] == 'Brother printer'
    assert '192.0.2.61' not in names  # nothing known about it: shown by address alone
    html = client.get('/').get_data(as_text=True)
    assert 'shellyplug-s-kitchen' in html and 'Brother printer' in html
    assert 'class="sentinel-tile"' in html


def test_nameless_devices_are_shown_by_address(scan_files):
    groups = network_scan.describe(run_fake_scan(), {'SENTINEL_HOSTS': ''}, {})
    entry = next(e for e in groups['good'] if e['ip'] == '192.0.2.61')
    assert entry['title'] == '192.0.2.61'


def test_names_cannot_break_the_settings_file():
    text = network_scan.format_sentinel_names({'192.0.2.5': {'title': 'Plug "x"; a=b|c', 'icon': 'plug',
                                                             'maker': 'Ma"ker'}})
    assert '"' not in text and text.count(';') == 0 and text.count('|') == 2
    assert network_scan.parse_sentinel_names(text)['192.0.2.5']['title'] == 'Plug x a b c'
