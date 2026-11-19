#!/usr/bin/env python3
"""Delete rotated log files older than the chosen number of days.

Log rotation keeps up to 30 days of old files (see logrotate/). This runs once
a night from cron and deletes the ones older than LOG_RETENTION_DAYS in
power_manager.conf (3, 7, 14 or 30; 7 when unset), as chosen in
Settings > General > Troubleshooting.

Only rotated copies are touched (power_manager.log.1, syslog.2.gz, ...), never
the log files currently being written.
"""

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config_store  # noqa: E402

CONFIG_FILE = "/etc/nut/power_manager.conf"
LOG_DIR = "/var/log"
CHOICES = (3, 7, 14, 30)
DEFAULT_DAYS = 7

ROTATED = re.compile(r'^(power_manager\.log|syslog|user\.log|mail\.log|kern\.log|auth\.log|'
                     r'cron\.log|daemon\.log|messages)\.\d+(\.gz)?$')


def retention_days(config):
    try:
        days = int(config.get('LOG_RETENTION_DAYS', DEFAULT_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return days if days in CHOICES else DEFAULT_DAYS


def prune(log_dir, days, now=None):
    """Delete rotated logs last written more than `days` days ago. Returns their names."""
    cutoff = (now or time.time()) - days * 86400
    removed = []
    try:
        names = os.listdir(log_dir)
    except OSError:
        return removed
    for name in sorted(names):
        path = os.path.join(log_dir, name)
        if not ROTATED.match(name) or not os.path.isfile(path):
            continue
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed.append(name)
        except OSError:
            pass
    return removed


def main():
    days = retention_days(config_store.read(CONFIG_FILE)[0])
    removed = prune(LOG_DIR, days)
    if removed:
        print(f"Removed {len(removed)} log file(s) older than {days} days: {', '.join(removed)}")


if __name__ == '__main__':
    main()
