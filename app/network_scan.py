"""Find devices on the local network, for choosing sentinels.

A scan pings every address in the server's /24 (the 254 addresses that share
its first three numbers), twice, then reads what the network already told the
server about each device that answered:

- its MAC address, from the kernel's neighbour table (/proc/net/arp), which
  fills in as a side effect of the pings;
- its name, from reverse DNS (your router or AdGuard usually knows it);
- its maker, from the first half of the MAC (app/data/oui_makers.tsv).

The pings are sent from inside this process when it is allowed to (one socket,
about 1-2 MB of memory). Otherwise it falls back to the ping command.

A scan runs in a background thread of whichever web worker started it. Its
progress and results go to a small file in /var/run/nut, so every worker can
answer "how is it going?". A lock file makes sure only one scan runs at a time;
the lock is released automatically if the worker stops.

Nothing is stored long term: the results file sits in /var/run, which is
emptied when the container restarts.
"""

import fcntl
import ipaddress
import json
import os
import queue
import random
import re
import select
import socket
import struct
import subprocess
import threading
import time

SCAN_FILE = "/var/run/nut/network_scan.json"
LOCK_FILE = "/var/run/nut/network_scan.lock"
MAKERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'oui_makers.tsv')
PING_CMD = "/bin/ping"
ARP_FILE = "/proc/net/arp"
ROUTE_FILE = "/proc/net/route"

FIRST_WAIT = 1.0      # seconds to wait for late answers after the first pass
SECOND_WAIT = 2.0     # the second pass gives slow or dozing devices longer
NAME_WAIT = 3.0       # how long to wait for names before showing results anyway
REUSE_SECONDS = 120   # a finished scan this recent is shown again instead of rescanning

# The sentinel list shipped in power_manager.conf.example: not a real choice.
EXAMPLE_SENTINELS = "192.168.1.11 192.168.1.12 192.168.1.13 192.168.1.14"


# --- facts about this server's network -------------------------------------------

def server_ip():
    """This server's own address on the LAN."""
    ip = os.environ.get('UPS_SERVER_HOST_IP', '').strip()
    if ip:
        return ip
    # Asking the kernel which address it would use; no packet is sent.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def gateway_ip():
    """The default gateway (usually your router), or None."""
    try:
        with open(ROUTE_FILE) as f:
            next(f)
            for line in f:
                fields = line.split()
                if len(fields) > 2 and fields[1] == '00000000':
                    return socket.inet_ntoa(struct.pack('<L', int(fields[2], 16)))
    except (OSError, ValueError, StopIteration):
        pass
    return None


def arp_table():
    """{ip: mac} for every neighbour the kernel currently has an answer for."""
    table = {}
    try:
        with open(ARP_FILE) as f:
            next(f)
            for line in f:
                fields = line.split()
                if len(fields) >= 4 and fields[2] != '0x0' and fields[3] != '00:00:00:00:00:00':
                    table[fields[0]] = fields[3].lower()
    except (OSError, StopIteration):
        pass
    return table


def reverse_name(ip):
    """The device's name from reverse DNS, without the local domain, or ''."""
    try:
        name = socket.gethostbyaddr(ip)[0]
    except (OSError, UnicodeError):
        return ''
    if not name or name == ip:
        return ''
    return name.split('.')[0]


def addresses_to_scan(own_ip):
    """The 254 neighbours in the server's /24, without the server itself."""
    try:
        net = ipaddress.ip_network(f'{own_ip}/24', strict=False)
    except ValueError:
        return []
    return [str(h) for h in net.hosts() if str(h) != own_ip]


# --- pinging -------------------------------------------------------------------

def _checksum(data):
    if len(data) % 2:
        data += b'\0'
    total = sum(struct.unpack(f'!{len(data) // 2}H', data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def echo_request(ident, seq):
    """An ICMP echo request ('ping') packet."""
    payload = b'ups-server sentinel scan'
    header = struct.pack('!BBHHH', 8, 0, 0, ident, seq)
    return struct.pack('!BBHHH', 8, 0, _checksum(header + payload), ident, seq) + payload


def parse_reply(packet, ident, has_ip_header):
    """The sender of an echo reply meant for us, or None."""
    offset = (packet[0] & 0x0F) * 4 if has_ip_header else 0
    if len(packet) < offset + 8:
        return None
    kind, _, _, reply_ident, _ = struct.unpack('!BBHHH', packet[offset:offset + 8])
    if kind != 0 or (has_ip_header and reply_ident != ident):
        return None
    return socket.inet_ntoa(packet[12:16]) if has_ip_header else True


def _open_socket():
    """(socket, has_ip_header). Raw needs root; 'ping sockets' may not."""
    try:
        return socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP), True
    except PermissionError:
        return socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP), False


def ping_sweep_socket(ips, wait, on_sent, on_reply):
    """Ping `ips` from one socket, a few at a time. Calls on_reply(ip) for each answer."""
    sock, has_ip_header = _open_socket()
    ident = random.randint(1, 0xFFFF)
    answered = set()
    sock.setblocking(False)

    def drain(until):
        while True:
            left = until - time.monotonic()
            if left <= 0:
                return
            ready, _, _ = select.select([sock], [], [], left)
            if not ready:
                return
            try:
                packet, addr = sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                continue
            who = parse_reply(packet, ident, has_ip_header)
            ip = who if has_ip_header else (addr[0] if who else None)
            if ip and ip in pending and ip not in answered:
                answered.add(ip)
                on_reply(ip)

    pending = set(ips)
    try:
        for start in range(0, len(ips), 8):
            for n, ip in enumerate(ips[start:start + 8], start):
                try:
                    sock.sendto(echo_request(ident, n & 0xFFFF), (ip, 0))
                except OSError:
                    pass  # e.g. the broadcast address; treat as silent
            on_sent(min(start + 8, len(ips)))
            drain(time.monotonic() + 0.05)
        drain(time.monotonic() + wait)
    finally:
        sock.close()
    return answered


def ping_sweep_command(ips, wait, on_sent, on_reply):
    """Fallback: run the ping command, 32 at a time."""
    answered, lock, done = set(), threading.Lock(), [0]
    work = queue.Queue()
    for ip in ips:
        work.put(ip)
    timeout = str(max(1, int(round(wait))))

    def worker():
        while True:
            try:
                ip = work.get_nowait()
            except queue.Empty:
                return
            try:
                ok = subprocess.run([PING_CMD, '-c', '1', '-W', timeout, ip],
                                    capture_output=True, timeout=wait + 2).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                ok = False
            with lock:
                done[0] += 1
                on_sent(done[0])
                if ok:
                    answered.add(ip)
            if ok:
                on_reply(ip)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(32, len(ips)) or 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return answered


def ping_sweep(ips, wait, on_sent, on_reply):
    try:
        return ping_sweep_socket(ips, wait, on_sent, on_reply)
    except OSError:
        return ping_sweep_command(ips, wait, on_sent, on_reply)


# --- makers ----------------------------------------------------------------------

def load_makers():
    """{'240AC4': ('Espressif', 'smart'), ...}. Loaded per scan, then let go."""
    makers = {}
    try:
        with open(MAKERS_FILE) as f:
            for line in f:
                if line.startswith('#'):
                    continue
                parts = line.rstrip('\n').split('\t')
                if len(parts) == 3:
                    makers[parts[0]] = (parts[1], parts[2])
    except OSError:
        pass
    return makers


def is_private_mac(mac):
    """Phones and laptops make up a random MAC per network ("private address").

    Those have the 'locally administered' bit set: the second hex digit is
    2, 6, A or E.
    """
    return bool(mac) and len(mac) > 1 and mac[1].lower() in '26ae'


# --- the scan --------------------------------------------------------------------

# Backend functions, replaceable in tests and in the sandbox.
backend = {
    'server_ip': server_ip,
    'gateway_ip': gateway_ip,
    'arp_table': arp_table,
    'reverse_name': reverse_name,
    'ping_sweep': ping_sweep,
}


def _write_state(state):
    tmp = f'{SCAN_FILE}.{os.getpid()}.{threading.get_ident()}.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f)
    os.replace(tmp, SCAN_FILE)


def _read_state():
    try:
        with open(SCAN_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _try_lock():
    """An open, locked file object, or None if a scan is already running."""
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    f = open(LOCK_FILE, 'a')
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def is_running():
    held = _try_lock()
    if held is None:
        return True
    held.close()
    return False


def run_scan(lock_handle=None):
    """Scan now, in this thread, writing progress to SCAN_FILE as it goes."""
    b = backend
    own = b['server_ip']()
    ips = addresses_to_scan(own) if own else []
    state = {'status': 'running', 'phase': 'first', 'started': time.time(),
             'server_ip': own, 'gateway_ip': b['gateway_ip'](),
             'total': len(ips), 'checked': 0, 'devices': {}}
    lock = threading.Lock()
    last_write = [0.0]
    names = queue.Queue()
    makers = load_makers()

    def save(force=False):
        now = time.monotonic()
        if force or now - last_write[0] > 0.25:
            last_write[0] = now
            _write_state(state)

    def attach_macs():
        table = b['arp_table']()
        for ip, mac in table.items():
            dev = state['devices'].get(ip)
            if dev is None and ip in ips:
                # Answered the "who has this address?" question but not the ping.
                dev = state['devices'][ip] = {'ip': ip, 'replied': False, 'name': '',
                                              'mac': '', 'maker': '', 'kind': ''}
                names.put(ip)
            if dev is not None and not dev['mac']:
                dev['mac'] = mac
                maker = makers.get(mac.replace(':', '')[:6].upper())
                if maker:
                    dev['maker'], dev['kind'] = maker

    def on_sent(count):
        with lock:
            if state['phase'] == 'first':
                state['checked'] = count
            save()

    def on_reply(ip):
        with lock:
            dev = state['devices'].get(ip)
            if dev is None:
                state['devices'][ip] = {'ip': ip, 'replied': True, 'name': '', 'mac': '',
                                        'maker': '', 'kind': ''}
                names.put(ip)
            else:
                dev['replied'] = True
            attach_macs()
            save()

    def name_worker():
        while True:
            ip = names.get()
            if ip is None:
                return
            name = b['reverse_name'](ip)
            if name:
                with lock:
                    if ip in state['devices']:
                        state['devices'][ip]['name'] = name
                    save()

    workers = [threading.Thread(target=name_worker, daemon=True) for _ in range(8)]
    for w in workers:
        w.start()

    try:
        save(force=True)
        b['ping_sweep'](ips, FIRST_WAIT, on_sent, on_reply)
        with lock:
            state['phase'] = 'second'
            state['checked'] = state['total']
            silent = [ip for ip in ips if not state['devices'].get(ip, {}).get('replied')]
            save(force=True)
        b['ping_sweep'](silent, SECOND_WAIT, lambda n: None, on_reply)
        with lock:
            attach_macs()
            state['phase'] = 'names'
            save(force=True)
        deadline = time.monotonic() + NAME_WAIT
        while not names.empty() and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        with lock:
            state['status'] = 'done'
            state['finished'] = time.time()
            save(force=True)
    except Exception as e:  # noqa: BLE001 - shown to the user as "the scan failed"
        with lock:
            state['status'] = 'failed'
            state['error'] = str(e)
            save(force=True)
    finally:
        for _ in workers:
            names.put(None)
        if lock_handle is not None:
            lock_handle.close()
    return state


def start(force=False):
    """Start a scan in the background unless one is running or just finished.

    Returns 'started', 'running' or 'recent'.
    """
    if not force:
        state = _read_state()
        if state and state.get('status') == 'done' and \
                time.time() - state.get('finished', 0) < REUSE_SECONDS:
            return 'recent'
    handle = _try_lock()
    if handle is None:
        return 'running'
    threading.Thread(target=run_scan, args=(handle,), name='sentinel-scan', daemon=True).start()
    return 'started'


def status():
    """The latest scan's raw state, or None. A 'running' scan whose worker died reads as failed."""
    state = _read_state()
    if state and state.get('status') == 'running' and not is_running():
        state['status'] = 'failed'
        state['error'] = 'The scan stopped unexpectedly.'
    return state


# --- turning facts into advice ---------------------------------------------------------

_PERSONAL = re.compile(r'iphone|ipad|android|galaxy|pixel|oneplus|redmi|poco|macbook|laptop|'
                       r'thinkpad|chromebook|surface|phone|tablet|kindle|watch', re.I)
_KINDS_BY_NAME = [
    ('light', re.compile(r'bulb|light|lamp|hue|lifx|wiz|yeelight|nanoleaf|govee', re.I)),
    ('smart', re.compile(r'esp[-_]?[0-9a-f]{4}|espressif|tasmota|shelly|sonoff|tuya|plug|socket|'
                         r'wemo|kasa|tapo|meross|smartlife|thermostat|ecobee|nest-?(protect|thermo)', re.I)),
    ('tv', re.compile(r'(^|[-_.])tv|tv($|[-_.\d])|roku|chromecast|fire-?tv|bravia|webos|apple-?tv|'
                      r'shield|android-?tv|television', re.I)),
    ('speaker', re.compile(r'sonos|echo|alexa|homepod|google-?home|nest-?(audio|mini|hub)|speaker', re.I)),
    ('printer', re.compile(r'printer|epson|brother|canon|officejet|laserjet|deskjet|envy|'
                           r'^hp[0-9a-f]{6}', re.I)),
    ('camera', re.compile(r'cam($|[-_\d])|camera|doorbell|^ring|reolink|hikvision|wyze|eufy', re.I)),
    ('console', re.compile(r'xbox|playstation|ps[45]|nintendo', re.I)),
    ('network', re.compile(r'router|openwrt|switch|unifi|mesh|repeater|extender|access-?point', re.I)),
    ('computer', re.compile(r'nas|synology|diskstation|qnap|truenas|proxmox|pve|server|'
                            r'raspberry|^pi($|[-\d])|homeassistant|desktop|-pc$', re.I)),
]

# Shown first: what is most likely always on and on ordinary wall power.
KIND_ORDER = ['smart', 'light', 'tv', 'speaker', 'camera', 'console', 'network', 'printer',
              'computer', '']
KIND_LABELS = {'smart': 'Smart home device', 'light': 'Smart light', 'tv': 'TV or streamer',
               'speaker': 'Speaker', 'camera': 'Camera', 'console': 'Games console',
               'network': 'Network device', 'printer': 'Printer', 'computer': 'Computer',
               '': 'Device'}
KIND_NOUNS = {'smart': 'smart device', 'light': 'light', 'tv': 'TV', 'speaker': 'speaker',
              'camera': 'camera', 'console': 'console', 'network': 'network device',
              'printer': 'printer', 'computer': 'computer', '': 'device'}
KIND_ICONS = {'smart': 'plug', 'light': 'lightbulb', 'tv': 'tv', 'speaker': 'speaker',
              'camera': 'cctv', 'console': 'gamepad-2', 'network': 'router', 'printer': 'printer',
              'computer': 'server', '': 'circle-dot', 'personal': 'smartphone'}


def guess_kind(device):
    name = device.get('name') or ''
    for kind, pattern in _KINDS_BY_NAME:
        if pattern.search(name):
            return kind
    return device.get('kind') or ''


def configured_sentinels(config):
    """The sentinel addresses in the config, ignoring the example file's placeholders."""
    raw = ' '.join(config.get('SENTINEL_HOSTS', '').replace(',', ' ').split())
    if raw == EXAMPLE_SENTINELS:
        return []
    return [ip.strip('"\'') for ip in raw.split() if ip.strip('"\'')]


def describe(state, config, wake_hosts):
    """Sort a scan into good choices and unsuitable ones, for the picker.

    Returns {'good': [...], 'unsuitable': [...]} with display-ready entries.
    Current sentinels always appear in 'good' (ticked), even if silent.
    """
    state = state or {}
    current = configured_sentinels(config)
    saved = parse_sentinel_names(config.get('SENTINEL_NAMES'))
    ups_ips = {h.get('IP'): h.get('NAME') or 'A protected machine' for h in wake_hosts.values()
               if h.get('IP')}
    own, gateway = state.get('server_ip'), state.get('gateway_ip')
    good, unsuitable = [], []
    devices = dict(state.get('devices', {}))
    for ip in current:
        devices.setdefault(ip, {'ip': ip, 'replied': False, 'name': '', 'mac': '', 'maker': '',
                                'kind': '', 'missing': True})

    for ip, dev in devices.items():
        kind = guess_kind(dev)
        reason = None
        if ip == own:
            reason = 'This server'
        elif ip == gateway:
            reason = 'Your router'
        elif ip in ups_ips:
            reason = 'On your UPS'
            kind = 'computer'
        elif is_private_mac(dev.get('mac')) or _PERSONAL.search(dev.get('name') or ''):
            reason = 'Phone or laptop'
            kind = 'personal'
        elif not dev.get('replied') and not dev.get('missing'):
            reason = "Doesn't answer pings"

        # The name the network knows; else "<maker> <kind>" ("Brother printer");
        # else what was saved when it was picked; else just the address.
        remembered = saved.get(ip, {})
        maker = dev.get('maker') or remembered.get('maker', '')
        icon = KIND_ICONS.get(kind, 'circle-dot')
        title = dev.get('name') or (ups_ips.get(ip) if ip in ups_ips else '')
        detail = ''
        if title:
            parts = [maker]
            if kind in KIND_LABELS and kind:
                parts.append(KIND_LABELS[kind])
            detail = ' · '.join(p for p in parts if p)
        elif dev.get('maker'):
            title = f"{dev['maker']} {KIND_NOUNS.get(kind if kind != 'personal' else '', 'device')}"
        elif remembered.get('title'):
            title = remembered['title']
            icon = remembered.get('icon') or icon
        else:
            title = ip

        entry = {'ip': ip, 'mac': dev.get('mac') or '', 'title': title, 'detail': detail,
                 'maker': maker, 'icon': icon, 'current': ip in current,
                 'missing': bool(dev.get('missing')), 'reason': reason,
                 'rank': KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER)}
        (good if reason is None or entry['current'] else unsuitable).append(entry)

    def ip_key(e):
        try:
            return int(ipaddress.ip_address(e['ip']))
        except ValueError:
            return 0

    good.sort(key=lambda e: (not e['current'], e['rank'], ip_key(e)))
    unsuitable.sort(key=ip_key)
    return {'good': good, 'unsuitable': unsuitable}


# --- remembering sentinels by MAC ------------------------------------------------------

def parse_sentinel_macs(value):
    """'192.0.2.5=aa:bb:.. 192.0.2.6=..' -> {ip: mac}."""
    out = {}
    for pair in (value or '').split():
        ip, _, mac = pair.partition('=')
        if ip and mac:
            out[ip] = mac.lower()
    return out


def format_sentinel_macs(macs):
    return ' '.join(f'{ip}={mac}' for ip, mac in macs.items() if ip and mac)


def _clean_label(text):
    """Safe inside one quoted config value: no quotes or separators, one line, short."""
    text = re.sub(r'["\'|;=\r\n\t]', ' ', str(text or ''))
    return ' '.join(text.split())[:60]


def parse_sentinel_names(value):
    """'192.0.2.5=Hall plug|plug|Espressif;...' -> {ip: {'title', 'icon', 'maker'}}."""
    out = {}
    for item in (value or '').split(';'):
        ip, _, rest = item.strip().partition('=')
        if not ip or not rest:
            continue
        title, icon, maker = (rest.split('|') + ['', '', ''])[:3]
        out[ip] = {'title': title, 'icon': icon if icon in KIND_ICONS.values() else 'circle-dot',
                   'maker': maker}
    return out


def format_sentinel_names(names):
    parts = []
    for ip, n in names.items():
        title, icon, maker = _clean_label(n.get('title')), n.get('icon') or '', _clean_label(n.get('maker'))
        if icon not in KIND_ICONS.values():
            icon = 'circle-dot'
        if title or maker or icon != 'circle-dot':
            parts.append(f'{ip}={title}|{icon}|{maker}')
    return ';'.join(parts)


def sentinel_labels(config):
    """What the dashboard shows for each sentinel: {ip: {'title', 'icon', 'maker', 'mac'}}."""
    names = parse_sentinel_names(config.get('SENTINEL_NAMES'))
    macs = parse_sentinel_macs(config.get('SENTINEL_MACS'))
    out = {}
    for ip in configured_sentinels(config):
        n = names.get(ip, {})
        out[ip] = {'title': n.get('title', ''), 'icon': n.get('icon', 'circle-dot'),
                   'maker': n.get('maker', ''), 'mac': macs.get(ip, '')}
    return out


def address_changes(config, table=None):
    """Sentinels whose address now belongs to another device, or that moved.

    Uses only what the server already knows (its neighbour table, refreshed by
    the power manager's regular pings), so it costs nothing.
    Returns [{'ip', 'new_ip' or None, 'replaced': bool}].
    """
    saved = parse_sentinel_macs(config.get('SENTINEL_MACS'))
    if not saved:
        return []
    table = backend['arp_table']() if table is None else table
    where = {mac: ip for ip, mac in table.items()}
    changes = []
    for ip in configured_sentinels(config):
        mac = saved.get(ip)
        if not mac:
            continue
        now = table.get(ip)
        new_ip = where.get(mac)
        if new_ip == ip:
            continue
        if now and now != mac:
            changes.append({'ip': ip, 'new_ip': new_ip, 'replaced': True})
        elif not now and new_ip:
            changes.append({'ip': ip, 'new_ip': new_ip, 'replaced': False})
    return changes
