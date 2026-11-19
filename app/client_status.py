"""Safe updates to client_status.json, the machines' latest heartbeats.

Written by the API (heartbeats from the machines' UPS scripts, several workers
at once) and by the power manager (e.g. marking a machine as woken). Writers
take turns on a side lock file and swap in a complete new copy, each through
its own temporary file, so no update overwrites another and readers never see
half a file.
"""

import fcntl
import json
import os


def update(path, ip, record):
    """Set the entry for `ip` to `record`, keeping every other machine's entry."""
    with open(path + '.lock', 'a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with open(path, 'r') as f:
                statuses = json.load(f)
        except (IOError, ValueError):
            statuses = {}
        statuses[ip] = record
        temp_file = f'{path}.{os.getpid()}.tmp'
        with open(temp_file, 'w') as f:
            json.dump(statuses, f, indent=2)
        os.replace(temp_file, path)
