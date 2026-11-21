"""What each machine does in a power cut, and what we know about its UPS script.

A machine with SHUTDOWN_DELAY_MINUTES turns itself off that many minutes into
a power cut. One without keeps running. UPS script 4.5.0 and later is told so
directly (SHUTDOWN_ENABLED=false in /config) and never shuts down. Older
scripts don't know that setting and need a delay, so they get the long-standing
default of 5 minutes and still turn off: the settings page and dashboard warn
about those machines.

Since 4.5.0 the script reports its version with every check-in
(client_version in client_status.json), which is how we tell the two apart.

Raspberry Pi boards can't be woken over the network: once halted they stay off
until their power is cycled. The makers list (app/data/oui_makers.tsv) tells us
which MAC addresses belong to one.
"""

import os
import re

# Keep in step with UPS_monitor's SCRIPT_VERSION.
KEEP_RUNNING_SINCE = (4, 5, 0)
# What /config sends scripts too old to understand "keep running".
OLD_SCRIPT_DELAY = '5'
# Statuses the UPS script itself reports (wol_* ones are written by the server).
AGENT_STATUSES = ('online', 'on_battery', 'shutdown_pending', 'shutting_down')

MAKERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'oui_makers.tsv')
NO_WAKE_MAKERS = ('Raspberry Pi',)


def turns_off(params):
    """True if the machine is set to turn itself off in a power cut."""
    return (params or {}).get('SHUTDOWN_DELAY_MINUTES') not in (None, '')


def parse_version(text):
    """'4.5.0' -> (4, 5, 0); anything else -> None."""
    match = re.fullmatch(r'\s*(\d+)\.(\d+)(?:\.(\d+))?\s*', str(text or ''))
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def has_reported(record):
    """True once the machine's UPS script has checked in at least once."""
    return (record or {}).get('status') in AGENT_STATUSES


def understands_keep_running(record):
    version = parse_version((record or {}).get('client_version'))
    return version is not None and version >= KEEP_RUNNING_SINCE


def old_script(params, record):
    """A "keep running" machine whose UPS script will still turn it off."""
    return not turns_off(params) and has_reported(record) and not understands_keep_running(record)


_no_wake_cache = []


def no_wake_prefixes():
    """MAC prefixes ('DCA632', ...) of boards that can't be woken over the network.

    Reads the makers list line by line and keeps only those few (a handful of
    short strings, kept for the life of the process), so the full list never
    sits in memory.
    """
    if _no_wake_cache:
        return list(_no_wake_cache)
    prefixes = []
    try:
        with open(MAKERS_FILE) as f:
            for line in f:
                parts = line.rstrip('\n').split('\t')
                if len(parts) == 3 and parts[1] in NO_WAKE_MAKERS:
                    prefixes.append(parts[0])
    except OSError:
        pass
    _no_wake_cache.extend(prefixes)
    return prefixes


def mac_prefix(mac):
    return re.sub(r'[^0-9A-Fa-f]', '', str(mac or ''))[:6].upper()


def cant_wake(mac, prefixes):
    return bool(mac) and mac_prefix(mac) in prefixes


def machine_notes(wake_hosts, client_statuses, prefixes):
    """Per machine, what the settings page should warn about.

    {section: {'old_script': bool, 'script_too_old': bool, 'cant_wake': bool,
               'no_script': bool, 'version': '4.5.0' or None}}

    old_script: set to keep running, but its script will still turn it off.
    script_too_old: its script couldn't keep it running, if it were set to.
    """
    notes = {}
    statuses = client_statuses if isinstance(client_statuses, dict) else {}
    for section, params in wake_hosts.items():
        record = statuses.get(params.get('IP', ''))
        notes[section] = {
            'old_script': old_script(params, record),
            'script_too_old': has_reported(record) and not understands_keep_running(record),
            'cant_wake': cant_wake(params.get('MAC'), prefixes),
            'no_script': turns_off(params) and not has_reported(record),
            'version': (record or {}).get('client_version') if has_reported(record) else None,
        }
    return notes
