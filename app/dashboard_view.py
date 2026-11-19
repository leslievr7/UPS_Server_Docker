#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Turns the raw state files into what the dashboard shows.

Everything the page needs - the banner sentence, the summary tiles, each
machine's status label and colour - is decided here, in one place, from plain
inputs. The templates only lay it out. Keeping this free of Flask and file I/O
makes the wording and the rules easy to read and to test.

Tones map to colours in the theme: success (green), warning (amber), danger
(red), info (blue) and muted (grey).
"""

from datetime import datetime, timedelta, timezone

WEEKDAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']


# --- small helpers -----------------------------------------------------------

def parse_utc(value):
    """Parse '2026-11-19T09:46:28Z' (optionally with microseconds) as UTC."""
    if not value:
        return None
    text = str(value)
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if dt else None


def ago(seconds):
    """'just now', '42 s ago', '3 min ago', '2 h ago'."""
    if seconds is None:
        return 'never'
    seconds = max(0, int(seconds))
    if seconds < 5:
        return 'just now'
    if seconds < 60:
        return f'{seconds} s ago'
    if seconds < 3600:
        return f'{seconds // 60} min ago'
    return f'{seconds // 3600} h ago'


def plural(n, word, many=None):
    return f"{n} {word if n == 1 else (many or word + 's')}"


def as_int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def is_true(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() == 'true'


def section_number(section):
    return as_int(section.rsplit('_', 1)[-1], 0)


# --- machines ----------------------------------------------------------------

AGENT_STATUSES = ('online', 'shutdown_pending', 'shutting_down')
# Logic keys off these internal names, never the visible label, so two states
# can share a word ("Offline") without being confused.
ATTENTION_KEYS = ('no_agent', 'stale', 'offline', 'wake_failed')
OFF_KEYS = ('off_expected', 'offline')


def machine_status(kind, reachable, record, age, stale_seconds, power_cut):
    """Decide a machine's label, tone and icon.

    Args:
        kind: 'client' (runs the UPS script) or 'wol' (Wake-on-LAN only).
        reachable: whether it answered a ping on the last check.
        record: its entry in client_status.json, or None.
        age: seconds since that entry was written, or None.
        stale_seconds: CLIENT_STALE_TIMEOUT_MINUTES in seconds.
        power_cut: True while the sentinels report an outage.
    """
    status = (record or {}).get('status')

    def result(key, label, tone, icon, note=None, pulse=False):
        return {'key': key, 'label': label, 'tone': tone, 'icon': icon, 'note': note, 'pulse': pulse}

    if status == 'shutdown_pending' and age is not None and age < stale_seconds:
        return result('countdown', 'Shutting down', 'warning', 'timer', pulse=True)
    if status == 'shutting_down':
        if age is not None and age < 180 and reachable:
            return result('stopping', 'Shutting down', 'danger', 'power-off', pulse=True)
        if power_cut and not reachable:
            return result('off_expected', 'Offline', 'muted', 'power-off',
                          'Turned off safely for the power cut.')
        if not reachable:
            return result('off_expected', 'Offline', 'muted', 'power-off',
                          'Turned off by its UPS script.')
    if status == 'wol_sent':
        if reachable:
            return result('booting', 'Booting', 'info', 'loader-circle', pulse=True)
        return result('wol_sent', 'Wake-on-LAN sent', 'info', 'send', 'Waiting for it to start up.')
    if status == 'wol_no_response':
        if reachable:
            return result('booting', 'Booting', 'info', 'loader-circle', pulse=True)
        return result('wake_failed', 'Did not wake up', 'danger', 'triangle-alert',
                      "Wake-on-LAN was sent but it never answered. Check its MAC address "
                      "and the network card's wake settings.")
    if status == 'wol_deferred' and not reachable:
        return result('wol_deferred', 'Waiting for charge', 'warning', 'battery-low',
                      'It will be woken once the battery has recharged enough.')
    if status == 'wol_failed' and not reachable:
        return result('wake_failed', 'Wake-on-LAN failed', 'danger', 'circle-x',
                      'The wake-up command could not be sent.')
    if status == 'wol_error' and not reachable:
        return result('wake_failed', 'Wake-on-LAN error', 'warning', 'circle-alert',
                      'Something went wrong while sending the wake-up signal.')

    if kind == 'wol':
        if reachable:
            return result('online', 'Online', 'success', 'circle-check')
        if power_cut:
            return result('off_expected', 'Offline', 'muted', 'power-off',
                          'Off because of the power cut.')
        # Someone switching off a Wake-on-LAN-only machine is normal.
        return result('off_expected', 'Offline', 'muted', 'circle-x')

    agent_fresh = status in AGENT_STATUSES and age is not None and age < stale_seconds
    if reachable:
        if agent_fresh:
            return result('online', 'Online', 'success', 'circle-check')
        if status in AGENT_STATUSES:
            return result('stale', 'Status stale', 'warning', 'clock',
                          "Its UPS script stopped checking in. It may not shut down in a power cut.")
        return result('no_agent', 'No agent', 'danger', 'wrench',
                      "It answers pings, but its UPS script has never checked in, so it "
                      "won't shut down in a power cut.")
    if power_cut:
        return result('off_expected', 'Offline', 'muted', 'power-off',
                      'Turned off for the power cut.')
    return result('offline', 'Offline', 'danger', 'circle-x', 'It does not answer pings.')


VERDICT_WORDS = {'OL': 'Keep running', 'OB': 'Keep running on battery', 'OB LB': 'Shut down'}


def build_machines(wake_hosts, client_statuses, reach_hosts, power_state, stale_seconds,
                   power_cut, battery_enabled, now, restoring=False, default_source='sentinel'):
    machines = []
    verdicts = (power_state or {}).get('hosts') or {}
    for section in sorted(wake_hosts, key=section_number):
        params = wake_hosts[section]
        ip = params.get('IP', '')
        if not ip:
            continue
        kind = 'client' if 'SHUTDOWN_DELAY_MINUTES' in params else 'wol'
        reachable = bool(reach_hosts.get(ip))
        record = client_statuses.get(ip) if isinstance(client_statuses, dict) else None
        stamp = parse_utc((record or {}).get('timestamp'))
        age = (now - stamp).total_seconds() if stamp else None

        status = machine_status(kind, reachable, record, age, stale_seconds, power_cut)
        auto_wol = params.get('AUTO_WOL', 'true').strip().lower() != 'false'
        if restoring and not reachable and status['key'] in OFF_KEYS:
            if auto_wol and params.get('MAC'):
                status = {'key': 'waiting_wake', 'label': 'Waiting to wake', 'tone': 'info',
                          'icon': 'clock', 'pulse': False,
                          'note': 'It will be woken when Wake-on-LAN goes out.'}
            else:
                status = {'key': 'off_expected', 'label': 'Offline', 'tone': 'muted',
                          'icon': 'power-off', 'pulse': False,
                          'note': 'Automatic wake-up is off for this machine. Use Wake to start it.'}

        countdown = None
        if status['key'] == 'countdown':
            remaining = as_int(record.get('remaining_seconds'), 0) - int(age or 0)
            total = as_int(record.get('shutdown_delay'), 0) * 60
            countdown = {
                'deadline': iso(now + timedelta(seconds=max(0, remaining))),
                'total': total,
                'remaining': max(0, remaining),
            }

        agent_seen = None
        agent_text = 'Never reported'
        if kind == 'client' and record and record.get('status') in AGENT_STATUSES:
            agent_seen = iso(stamp)
            seen_age = (now - stamp).total_seconds()
            # Only say when it last reported if that is worth worrying about;
            # a ticking "x s ago" on every card is noise.
            agent_text = 'Reporting' if seen_age <= stale_seconds else f'Last heard {ago(seen_age)}'

        delay = as_int(params.get('SHUTDOWN_DELAY_MINUTES'), None) if kind == 'client' else None
        # Battery rules only matter for machines set to use them; for the rest
        # the verdict just repeats what the sentinels say.
        source = (params.get('POWER_SOURCE') or default_source or 'sentinel').strip().lower()
        verdict = None
        if battery_enabled and kind == 'client' and source in ('battery', 'both') and verdicts.get(ip):
            raw = verdicts[ip]
            verdict = {
                'decision': VERDICT_WORDS.get(raw.get('would_be'), raw.get('would_be')),
                'observe': raw.get('mode') == 'observe',
                'reason': raw.get('reason', ''),
                'disagreement': bool(raw.get('disagreement')),
            }

        machines.append({
            'section': section,
            'name': params.get('NAME') or section,
            'ip': ip,
            'kind': kind,
            'icon': 'server' if kind == 'client' else 'monitor',
            'reachable': reachable,
            'status': status,
            'countdown': countdown,
            'agent_seen': agent_seen,
            'agent_text': agent_text,
            'delay_text': None if delay is None else ('Immediately' if delay == 0 else f'After {delay} min'),
            'auto_wol': auto_wol,
            'ignores_test_mode': is_true(params.get('IGNORE_SIMULATION')),
            # Waking a machine during a power cut would only drain the UPS.
            'can_wake': bool(params.get('MAC')) and not reachable and not power_cut,
            'has_mac': bool(params.get('MAC')),
            'verdict': verdict,
            # Problems a person should look at - not the shutdowns a power cut
            # is supposed to cause.
            'needs_attention': status['key'] in ATTENTION_KEYS,
        })
    return machines


# --- banner ------------------------------------------------------------------

def build_banner(power_state, simulation, manager_state, machines, sentinels_online,
                 sentinels_total, wol_delay, checked_text, now_local):
    clients = [m for m in machines if m['kind'] == 'client']
    attention = [m for m in machines if m['needs_attention']]
    state_name = (manager_state or {}).get('STATE')
    state_since = as_int((manager_state or {}).get('TIMESTAMP'), 0)
    since_local = datetime.fromtimestamp(state_since) if state_since else None

    if power_state is None:
        return {
            'tone': 'warning', 'icon': 'circle-alert',
            'title': "Can't confirm the power state",
            'text': "The power monitor hasn't reported in the last 90 seconds. "
                    "Machines keep their last instructions until it does.",
        }

    if simulation:
        acting = [m for m in clients if not m['ignores_test_mode']]
        text = "Machines are being told the power is out. "
        if acting:
            text += (f"{plural(len(acting), 'machine')} without “ignore test mode” "
                     f"{'is' if len(acting) == 1 else 'are'} shutting down.")
        else:
            text += "Every machine ignores test mode, so nothing shuts down."
        return {'tone': 'warning', 'icon': 'flask-conical', 'title': 'Test mode is on',
                'text': text, 'action': 'test_mode'}

    if sentinels_total and sentinels_online == 0:
        counting = [m for m in clients if m['countdown']]
        down = [m for m in clients if m['status']['key'] in OFF_KEYS]
        parts = []
        if since_local and state_name == 'POWER_FAIL':
            parts.append(f"No sentinel has answered since {since_local:%H:%M}.")
        else:
            parts.append("No sentinel is answering.")
        if counting:
            parts.append(f"{plural(len(counting), 'machine')} counting down.")
        if down:
            parts.append(f"{plural(len(down), 'machine')} already off.")
        return {'tone': 'danger', 'icon': 'unplug',
                'title': 'Power cut — shutting machines down', 'text': ' '.join(parts)}

    if state_name in ('POWER_RESTORED', 'POWER_RESTORED_SIM'):
        text = f"Mains returned at {since_local:%H:%M}. " if since_local else "Mains is back. "
        wake_at = since_local + timedelta(minutes=wol_delay) if since_local else None
        if wake_at and wake_at > now_local:
            minutes = max(1, int((wake_at - now_local).total_seconds() // 60 + 1))
            title = f"Power is back — waking machines in {minutes} min"
            text += f"Wake-on-LAN goes out at {wake_at:%H:%M}."
        else:
            title = 'Power is back — waking machines'
            text += "Machines that were shut down are being woken up."
        return {'tone': 'info', 'icon': 'refresh-cw', 'title': title, 'text': text}

    battery = (power_state or {}).get('battery') or {}
    if battery.get('enabled') and battery.get('available') and battery.get('ac_power') is False:
        return {'tone': 'warning', 'icon': 'battery-warning',
                'title': 'The UPS is running on battery',
                'text': "The sentinels still answer, so grid power looks fine but the supply to "
                        "the UPS itself seems cut. A tripped breaker is the usual cause."}

    if sentinels_total == 0:
        text = "No sentinels are set up, so power cuts can't be detected. The server can find some for you."
        return {'tone': 'warning', 'icon': 'circle-alert', 'title': 'Power cuts are not being watched',
                'text': text, 'action': 'find_sentinels'}

    if sentinels_online == sentinels_total:
        text = f"All {sentinels_total} sentinels are answering."
    else:
        text = f"{sentinels_online} of {sentinels_total} sentinels are answering."
    if attention:
        text += f" {plural(len(attention), 'machine')} {'needs' if len(attention) == 1 else 'need'} attention."
    else:
        text += f" {plural(len(clients), 'machine')} protected."
    return {'tone': 'success', 'icon': 'plug-zap', 'title': 'Mains power is on', 'text': text}


# --- tiles -------------------------------------------------------------------

def next_test(schedules, now_local):
    """The next enabled 'start' schedule, as (datetime, name), or None."""
    best = None
    for section, params in (schedules or {}).items():
        if not is_true(params.get('ENABLED')) or params.get('ACTION', '').lower() != 'start':
            continue
        try:
            hour, minute = (int(x) for x in params.get('TIME', '').split(':'))
        except ValueError:
            continue
        candidates = []
        if params.get('TYPE') == 'one-time':
            try:
                day = datetime.strptime(params.get('DATE', ''), '%Y-%m-%d')
            except ValueError:
                continue
            candidates.append(day.replace(hour=hour, minute=minute))
        else:
            dow = params.get('DAY_OF_WEEK', '').lower()
            for offset in range(8):
                day = now_local + timedelta(days=offset)
                if dow == 'everyday' or WEEKDAYS[day.weekday()] == dow:
                    candidates.append(day.replace(hour=hour, minute=minute, second=0, microsecond=0))
        for when in candidates:
            if when > now_local and (best is None or when < best[0]):
                best = (when, params.get('NAME') or section)
    return best


def build_tiles(power_state, machines, sentinels_online, sentinels_total, wol_delay,
                checked_at, checked_text, schedules, now_local):
    clients = [m for m in machines if m['kind'] == 'client']
    attention = [m for m in clients if m['needs_attention']]
    tiles = [
        {'label': 'Sentinels answering', 'icon': 'radio-tower',
         'value': str(sentinels_online) if power_state else '—',
         'suffix': f'of {sentinels_total}' if power_state else None,
         'hint': ('All answering' if sentinels_online == sentinels_total
                  else f'{sentinels_total - sentinels_online} not answering') if power_state else None,
         'hint_tone': 'danger' if power_state and sentinels_online < sentinels_total else None},
        {'label': 'Machines protected', 'icon': 'server', 'value': str(len(clients)),
         'hint': (f"{len(attention)} {'needs' if len(attention) == 1 else 'need'} attention"
                  if attention else 'All reporting normally'),
         'hint_tone': 'danger' if attention else None},
    ]

    battery = (power_state or {}).get('battery') or {}
    if battery.get('enabled'):
        if battery.get('available') and battery.get('soc') is not None:
            details = []
            if battery.get('voltage') is not None:
                details.append(f"{battery['voltage']:.1f} V")
            current = battery.get('current')
            if battery.get('ac_power') is False:
                details.append('on battery')
            elif current is not None and current > 0.5:
                details.append('charging')
            runtime = battery.get('runtime_to_shutdown_mins')
            if runtime is None:
                runtime = battery.get('remaining_mins')
            if battery.get('ac_power') is False and runtime is not None:
                details.append(f'~{runtime} min left')
            tiles.append({'label': 'Battery', 'icon': 'battery-charging' if battery.get('ac_power') else 'battery-low',
                          'value': f"{battery['soc']:.0f}%", 'hint': ' · '.join(details),
                          'hint_tone': 'warning' if battery.get('ac_power') is False else None})
        else:
            tiles.append({'label': 'Battery', 'icon': 'battery-warning', 'value': '—',
                          'hint': 'Monitor unavailable', 'hint_tone': 'warning'})

    tiles.append({'label': 'Wake-up delay', 'icon': 'clock', 'value': str(wol_delay), 'suffix': 'min',
                  'hint': 'after power returns'})

    upcoming = next_test(schedules, now_local)
    if upcoming:
        when, name = upcoming
        same_day = when.date() == now_local.date()
        tiles.append({'label': 'Next test', 'icon': 'calendar-clock',
                      'value': when.strftime('%H:%M') if same_day else when.strftime('%a %H:%M'),
                      'hint': name})
    else:
        tiles.append({'label': 'Next test', 'icon': 'calendar-clock', 'value': 'None',
                      'hint': 'No test outage scheduled'})
    return tiles


# --- everything --------------------------------------------------------------

def build_dashboard(config, wake_hosts, schedules, power_state, client_statuses,
                    manager_state, reach_sentinels, reach_hosts, now=None, now_local=None):
    """Assemble the whole dashboard view.

    reach_sentinels / reach_hosts are {ip: bool}; the caller fills in any
    address the power manager did not check.
    """
    now = now or datetime.now(timezone.utc)
    now_local = now_local or datetime.now()

    sentinel_ips = [ip.strip('"\'') for ip in config.get('SENTINEL_HOSTS', '').split() if ip.strip('"\'')]
    sentinels = [{'ip': ip, 'up': bool(reach_sentinels.get(ip))} for ip in sentinel_ips]
    sentinels_online = sum(1 for s in sentinels if s['up'])
    sentinels_total = len(sentinels)
    power_cut = sentinels_total > 0 and sentinels_online == 0 and power_state is not None

    reach = (power_state or {}).get('reachability') or {}
    checked = parse_utc(reach.get('checked_at') or (power_state or {}).get('updated_at'))
    checked_text = ago((now - checked).total_seconds()) if checked else 'never'

    simulation = is_true(config.get('POWER_SIMULATION_MODE'))
    battery = (power_state or {}).get('battery') or {}
    battery_enabled = bool(battery.get('enabled'))
    stale_seconds = max(1, as_int(config.get('CLIENT_STALE_TIMEOUT_MINUTES'), 5)) * 60
    wol_delay = as_int(config.get('WOL_DELAY_MINUTES'), 5)

    restoring = (manager_state or {}).get('STATE') in ('POWER_RESTORED', 'POWER_RESTORED_SIM')
    machines = build_machines(wake_hosts, client_statuses or {}, reach_hosts, power_state,
                              stale_seconds, power_cut and not simulation, battery_enabled, now,
                              restoring=restoring and not power_cut,
                              default_source=config.get('BATTERY_DEFAULT_POWER_SOURCE', 'sentinel'))

    observe_note = None
    if battery_enabled and (power_state or {}).get('decision_mode') == 'observe':
        if any((m['verdict'] or {}).get('disagreement') for m in machines):
            observe_note = ("The battery rules would decide differently for some machines, but they are "
                            "in observe mode: machines still follow the sentinels.")

    return {
        'banner': build_banner(power_state, simulation, manager_state, machines, sentinels_online,
                               sentinels_total, wol_delay, checked_text, now_local),
        'tiles': build_tiles(power_state, machines, sentinels_online, sentinels_total, wol_delay,
                             iso(checked), checked_text, schedules, now_local),
        'clients': [m for m in machines if m['kind'] == 'client'],
        'others': [m for m in machines if m['kind'] == 'wol'],
        'sentinels': sentinels,
        'sentinels_online': sentinels_online,
        'checked_at': iso(checked),
        'checked_text': checked_text,
        'checked_local': checked.astimezone().strftime('%H:%M:%S') if checked else None,
        # The watchman checks every 15 s; over 90 s of silence means it has stopped.
        'watch_stale': checked is None or (now - checked).total_seconds() > 90,
        'simulation': simulation,
        'power_cut': power_cut,
        'battery_enabled': battery_enabled,
        'observe_note': observe_note,
    }
