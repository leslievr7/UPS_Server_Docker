#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Check whether a set of hosts answers a ping, all at once.

The power manager uses this to record which machines are up on every pass and
publish the result in power_state.json, so the Web GUI can show it without
pinging anything itself. Pinging inside a web request made each dashboard
refresh wait for every unreachable host in turn - during a power cut, exactly
when the page was needed, a refresh took longer than the 5 s polling interval
and the requests piled up.

Pings run in parallel, so the whole check costs about one ping timeout however
many hosts are down. Nothing here makes a decision; it only observes.
"""

import subprocess
import time
from datetime import datetime

PING_CMD = "/bin/ping"


def check(ips, timeout_seconds=1, ping_cmd=None):
    """Ping every address once, in parallel.

    Args:
        ips: iterable of IP addresses; duplicates and blanks are ignored.
        timeout_seconds: how long each ping waits for a reply.
        ping_cmd: override for the ping binary (tests and the sandbox).

    Returns:
        {ip: True/False} - False also covers "could not run ping at all".
    """
    cmd = ping_cmd or PING_CMD
    processes = {}
    for ip in dict.fromkeys(ip for ip in ips if ip):
        try:
            processes[ip] = subprocess.Popen(
                [cmd, "-c", "1", "-W", str(timeout_seconds), ip],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            processes[ip] = None

    deadline = time.monotonic() + timeout_seconds + 2
    results = {}
    for ip, process in processes.items():
        if process is None:
            results[ip] = False
            continue
        try:
            remaining = max(0.1, deadline - time.monotonic())
            results[ip] = process.wait(timeout=remaining) == 0
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            results[ip] = False
    return results


def utc_now():
    """Timestamp in the same format the rest of power_state.json uses."""
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
