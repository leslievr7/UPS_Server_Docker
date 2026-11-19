"""reachability.check() pings in parallel, so offline hosts don't add up."""

import os
import stat
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app'))

import reachability  # noqa: E402


def make_fake_ping(tmp_path, answering):
    """A ping stand-in that takes 0.8 s and answers only for `answering`."""
    script = tmp_path / 'slow-ping'
    allowed = ' '.join(answering)
    script.write_text(
        '#!/bin/sh\n'
        'for last; do :; done\n'
        'sleep 0.8\n'
        f'for ip in {allowed}; do [ "$ip" = "$last" ] && exit 0; done\n'
        'exit 1\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_results_per_address(tmp_path):
    ping = make_fake_ping(tmp_path, ['192.0.2.1'])
    result = reachability.check(['192.0.2.1', '192.0.2.2', '', '192.0.2.1'], ping_cmd=ping)
    assert result == {'192.0.2.1': True, '192.0.2.2': False}


def test_offline_hosts_are_checked_in_parallel(tmp_path):
    ping = make_fake_ping(tmp_path, [])
    ips = [f'192.0.2.{n}' for n in range(1, 9)]
    started = time.monotonic()
    result = reachability.check(ips, ping_cmd=ping)
    elapsed = time.monotonic() - started
    assert set(result.values()) == {False}
    # Eight 0.8 s pings one after another would take 6.4 s.
    assert elapsed < 2.5


def test_missing_ping_binary_reports_unreachable():
    result = reachability.check(['192.0.2.1'], ping_cmd='/nonexistent/ping')
    assert result == {'192.0.2.1': False}
