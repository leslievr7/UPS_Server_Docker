"""Saving power_manager.conf safely.

Writers take turns, saves never leave a half-written file, and only the
settings that changed are touched: comments, unknown settings and changes
made meanwhile by the power manager all survive.
"""

import copy
import os
import stat
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'app'))

import config_store  # noqa: E402
import power_manager  # noqa: E402
import web_auth  # noqa: E402
import web_gui  # noqa: E402

EXAMPLE = os.path.join(ROOT, 'config', 'power_manager.conf.example')

SMALL = '''# My settings
SENTINEL_HOSTS="192.0.2.101 192.0.2.102"
POWER_SIMULATION_MODE="false"
FUTURE_SETTING="kept"

# === MACHINES ===

[WAKE_HOST_1]
NAME="Proxmox"
IP="192.0.2.10"
# its NIC is the onboard one
MAC="02:00:00:00:00:10"

# === SCHEDULES ===

[SCHEDULE_1]
NAME="Weekly test"
ENABLED="true"
'''


@pytest.fixture
def conf(tmp_path):
    path = tmp_path / 'power_manager.conf'
    path.write_text(SMALL)
    return path


def changed_lines(before, after):
    a, b = before.splitlines(), after.splitlines()
    return [line for line in b if line not in a], [line for line in a if line not in b]


# --- reading ---------------------------------------------------------------------

@pytest.mark.parametrize('text', [open(EXAMPLE).read(), SMALL])
def test_parser_matches_the_power_manager(tmp_path, monkeypatch, text):
    path = tmp_path / 'power_manager.conf'
    path.write_text(text + '\n[SOMETHING_ELSE]\nODD_KEY="1"\n')
    monkeypatch.setattr(power_manager, 'CONFIG_FILE', str(path))
    assert config_store.read(str(path)) == power_manager.read_power_manager_config()


def test_missing_file_reads_as_empty(tmp_path):
    assert config_store.read(str(tmp_path / 'nope.conf')) == ({}, {}, {})


# --- only what changed is touched --------------------------------------------------

def test_one_change_to_the_example_touches_one_line(tmp_path):
    path = tmp_path / 'power_manager.conf'
    original = open(EXAMPLE).read()
    path.write_text(original)
    base = config_store.read(str(path))
    new = copy.deepcopy(base)
    new[0]['WOL_DELAY_MINUTES'] = '7'
    assert config_store.save(str(path), new, base=base)
    added, removed = changed_lines(original, path.read_text())
    assert added == ['WOL_DELAY_MINUTES="7"']
    assert removed == ['WOL_DELAY_MINUTES=5']


def test_unchanged_save_does_not_rewrite(conf):
    before = os.stat(conf)
    base = config_store.read(str(conf))
    assert not config_store.save(str(conf), copy.deepcopy(base), base=base)
    after = os.stat(conf)
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert not os.path.exists(str(conf) + '.bak')


def test_comments_and_unknown_settings_survive(conf):
    base = config_store.read(str(conf))
    new = copy.deepcopy(base)
    new[0]['SENTINEL_HOSTS'] = '192.0.2.103'
    new[1]['WAKE_HOST_1']['NAME'] = 'Proxmox host'
    config_store.save(str(conf), new, base=base)
    text = conf.read_text()
    for line in ('# My settings', '# its NIC is the onboard one', '# === SCHEDULES ===',
                 'FUTURE_SETTING="kept"'):
        assert line in text
    assert config_store.read(str(conf))[1]['WAKE_HOST_1']['NAME'] == 'Proxmox host'


def test_without_a_base_the_file_ends_up_matching(conf):
    target = ({'SENTINEL_HOSTS': '192.0.2.9'}, {}, {'SCHEDULE_1': {'NAME': 'x'}})
    config_store.save(str(conf), target)
    assert config_store.read(str(conf)) == target


# --- changes made meanwhile survive --------------------------------------------------

def test_power_manager_change_made_after_the_page_loaded_is_kept(conf):
    # The page loads while test mode is on...
    config_store.set_value(str(conf), 'POWER_SIMULATION_MODE', 'true')
    base = config_store.read(str(conf))
    # ...a schedule then switches it off...
    config_store.set_value(str(conf), 'POWER_SIMULATION_MODE', 'false')
    # ...and the user saves an unrelated setting from that page.
    new = copy.deepcopy(base)
    new[0]['SENTINEL_HOSTS'] = '192.0.2.103'
    config_store.save(str(conf), new, base=base)
    config = config_store.read(str(conf))[0]
    assert config['POWER_SIMULATION_MODE'] == 'false'
    assert config['SENTINEL_HOSTS'] == '192.0.2.103'


def test_web_save_in_a_request_keeps_a_meanwhile_change(conf, monkeypatch):
    monkeypatch.setattr(web_gui, 'POWER_MANAGER_CONFIG', str(conf))
    monkeypatch.setattr(web_auth, 'AUTH_FILE', str(conf.parent / 'web_auth.json'))
    monkeypatch.setattr(web_auth, '_cache', {'mtime': None, 'data': {}})
    config_store.set_value(str(conf), 'POWER_SIMULATION_MODE', 'true')
    with web_gui.app.test_request_context('/settings/notifications', method='POST'):
        config, hosts, schedules = web_gui.read_power_manager_config()
        config_store.set_value(str(conf), 'POWER_SIMULATION_MODE', 'false')
        config['SMTP_SERVER'] = 'smtp.example.com'
        web_gui.write_power_manager_config(config, hosts, schedules)
    config = config_store.read(str(conf))[0]
    assert config['POWER_SIMULATION_MODE'] == 'false'
    assert config['SMTP_SERVER'] == 'smtp.example.com'


def test_two_tabs_adding_a_machine_keep_both(conf):
    base = config_store.read(str(conf))
    for name in ('Pi', 'NAS'):
        new = copy.deepcopy(base)
        new[1]['WAKE_HOST_2'] = {'NAME': name, 'IP': '192.0.2.20', 'MAC': '02:00:00:00:00:20'}
        config_store.save(str(conf), new, base=base)
    hosts = config_store.read(str(conf))[1]
    assert hosts['WAKE_HOST_2']['NAME'] == 'Pi'
    assert hosts['WAKE_HOST_3']['NAME'] == 'NAS'


def test_deleted_machine_is_not_brought_back(conf):
    base = config_store.read(str(conf))
    deleted = copy.deepcopy(base)
    del deleted[1]['WAKE_HOST_1']
    config_store.save(str(conf), deleted, base=base)
    # A second tab, still showing the machine, edits it.
    edited = copy.deepcopy(base)
    edited[1]['WAKE_HOST_1']['NAME'] = 'Renamed'
    config_store.save(str(conf), edited, base=base)
    assert 'WAKE_HOST_1' not in config_store.read(str(conf))[1]
    assert not config_store.set_value(str(conf), 'ENABLED', 'false', section='WAKE_HOST_1')
    assert '[WAKE_HOST_1]' not in conf.read_text()


def test_deleting_a_section_keeps_the_next_heading(conf):
    base = config_store.read(str(conf))
    new = copy.deepcopy(base)
    del new[1]['WAKE_HOST_1']
    config_store.save(str(conf), new, base=base)
    text = conf.read_text()
    assert '# === SCHEDULES ===' in text
    assert 'NAME="Proxmox"' not in text
    assert config_store.read(str(conf))[2]['SCHEDULE_1']['NAME'] == 'Weekly test'


# --- the power manager's single-setting saves --------------------------------------------

def test_new_main_setting_goes_with_the_main_settings(conf):
    # It used to be appended at the very end, inside the last schedule.
    config_store.set_value(str(conf), 'DEBUG_MODE', 'true')
    config, _, schedules = config_store.read(str(conf))
    assert config['DEBUG_MODE'] == 'true'
    assert 'DEBUG_MODE' not in schedules['SCHEDULE_1']


def test_section_setting_is_changed_in_place(conf):
    assert config_store.set_value(str(conf), 'ENABLED', 'false', section='SCHEDULE_1')
    assert config_store.read(str(conf))[2]['SCHEDULE_1']['ENABLED'] == 'false'
    assert conf.read_text().count('ENABLED=') == 1


# --- the file itself ---------------------------------------------------------------------

def test_backup_permissions_and_no_leftovers(conf):
    os.chmod(conf, 0o640)
    before = conf.read_text()
    config_store.set_value(str(conf), 'POWER_SIMULATION_MODE', 'true')
    assert open(str(conf) + '.bak').read() == before
    assert stat.S_IMODE(os.stat(conf).st_mode) == 0o640
    leftovers = [n for n in os.listdir(conf.parent) if n.endswith('.tmp')]
    assert leftovers == []


def test_writers_take_turns_and_readers_never_see_half_a_file(conf):
    errors = []
    stop = threading.Event()

    def watchman(n):
        for i in range(40):
            config_store.set_value(str(conf), f'KEY_{n}', str(i))

    def web_page():
        for i in range(40):
            base = config_store.read(str(conf))
            new = copy.deepcopy(base)
            new[0]['SENTINEL_HOSTS'] = f'192.0.2.{i}'
            config_store.save(str(conf), new, base=base)

    def reader():
        while not stop.is_set():
            config, hosts, _ = config_store.read(str(conf))
            if 'SENTINEL_HOSTS' not in config or 'WAKE_HOST_1' not in hosts:
                errors.append('incomplete read')

    threads = [threading.Thread(target=watchman, args=(n,)) for n in range(3)]
    threads.append(threading.Thread(target=web_page))
    readers = [threading.Thread(target=reader) for _ in range(2)]
    for t in threads + readers:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    for t in readers:
        t.join()

    config = config_store.read(str(conf))[0]
    assert errors == []
    assert [config[f'KEY_{n}'] for n in range(3)] == ['39'] * 3
    assert config['SENTINEL_HOSTS'] == '192.0.2.39'
    assert '# its NIC is the onboard one' in conf.read_text()
