#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sandbox: run the whole UPS server on a laptop, against a pretend world.

Starts, in one process:
  - the Web GUI            http://127.0.0.1:8080   (control panel at /dev)
  - the UPS Hub API        http://127.0.0.1:5050
  - a fake Victron battery monitor on 127.0.0.1:5088
  - the power manager loop (one pass every 15 s, like cron + sub-minute polling)
  - pretend UPS_monitor agents that poll the API exactly like the real client

Nothing here can reach a real machine:
  - ping, Wake-on-LAN and upsc are replaced by the scripts in dev/bin/, which
    only read and write files under the sandbox folder;
  - every address is in 192.0.2.0/24 (TEST-NET-1) and every MAC is a dummy;
  - emails are recorded in the control panel instead of being sent;
  - all servers listen on 127.0.0.1 only.

Production code is not modified. The sandbox re-points the modules' file and
command constants at dev/.sandbox/ after importing them.

Usage:
  .venv/bin/python dev/run_local.py            # keep sandbox settings, fresh runtime
  .venv/bin/python dev/run_local.py --reset    # start again from dev/test_config.conf
  .venv/bin/python dev/run_local.py --first-run  # brand-new install: no password, no sentinels
"""

import argparse
import collections
import json
import logging
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_lan  # noqa: E402  (the pretend home network)
from datetime import datetime, timezone

DEV_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(DEV_DIR)
APP_DIR = os.path.join(REPO_DIR, 'app')
BIN_DIR = os.path.join(DEV_DIR, 'bin')
SANDBOX = os.environ.get('UPS_SANDBOX') or os.path.join(DEV_DIR, '.sandbox')

WEB_PORT = int(os.environ.get('UPS_SANDBOX_WEB_PORT', '8080'))
API_PORT = int(os.environ.get('UPS_SANDBOX_API_PORT', '5050'))
VBM_PORT = int(os.environ.get('UPS_SANDBOX_VBM_PORT', '5088'))
PASS_SECONDS = float(os.environ.get('UPS_SANDBOX_PASS_SECONDS', '15'))
AGENT_SECONDS = 15
BOOT_SECONDS = 20       # how long a pretend machine takes to boot after WoL
SHUTDOWN_SECONDS = 8    # how long a pretend machine takes to power off
TOKEN = 'sandbox-test-token'

# The modules read these at import time or call time.
os.environ['UPS_SANDBOX'] = SANDBOX
os.environ['UPS_SERVER_HOST_IP'] = '127.0.0.1'

PATHS = {
    '/etc/nut/': os.path.join(SANDBOX, 'etc', 'nut'),
    '/var/run/nut/': os.path.join(SANDBOX, 'run', 'nut'),
    '/var/log/': os.path.join(SANDBOX, 'log'),
}
WORLD_DIR = os.path.join(SANDBOX, 'world')
CONFIG_PATH = os.path.join(PATHS['/etc/nut/'], 'power_manager.conf')


# --- Sandbox folder ------------------------------------------------------

SANDBOX_PASSWORD = 'sandbox-admin'  # test value, sandbox only


def prepare_sandbox(reset, first_run=False):
    if (reset or first_run) and os.path.isdir(SANDBOX):
        shutil.rmtree(SANDBOX)
    for path in list(PATHS.values()) + [WORLD_DIR]:
        os.makedirs(path, exist_ok=True)

    if not os.path.exists(CONFIG_PATH):
        with open(os.path.join(DEV_DIR, 'test_config.conf')) as f:
            template = f.read()
        template = template.replace('@SANDBOX@', SANDBOX)
        if first_run:
            template = '\n'.join(l for l in template.splitlines() if not l.startswith('SENTINEL_HOSTS=')) + '\n'
        with open(CONFIG_PATH, 'w') as f:
            f.write(template)

    # Sign-in for the sandbox only. The test password is documented in
    # dev/README.md; delete this file to try the first-run setup page instead.
    auth_file = os.path.join(PATHS['/etc/nut/'], 'web_auth.json')
    if not os.path.exists(auth_file) and not first_run:
        import json
        import secrets
        from werkzeug.security import generate_password_hash
        with open(auth_file, 'w') as f:
            json.dump({'secret_key': secrets.token_hex(32),
                       'password_hash': generate_password_hash(SANDBOX_PASSWORD, method='pbkdf2:sha256:200000')},
                      f, indent=2)

    ups_conf = os.path.join(PATHS['/etc/nut/'], 'ups.conf')
    if not os.path.exists(ups_conf):
        with open(ups_conf, 'w') as f:
            f.write("[ups]\n    driver = dummy-ups\n    port = virtual.device\n"
                    "    desc = \"Sandbox UPS\"\n")

    reset_runtime()


def reset_runtime():
    """Forget everything the power manager and API have learned so far."""
    run_dir = PATHS['/var/run/nut/']
    for name in os.listdir(run_dir):
        os.remove(os.path.join(run_dir, name))
    with open(os.path.join(run_dir, 'virtual.device'), 'w') as f:
        f.write("ups.status: OL\n")
    with open(os.path.join(run_dir, 'client_status.json'), 'w') as f:
        f.write('{}')
    for name in ('power_manager.state', 'notification.state', 'client_notification.state'):
        open(os.path.join(run_dir, name), 'a').close()
    open(os.path.join(WORLD_DIR, 'wake_requests.log'), 'w').close()


def remap(module):
    """Point a module's absolute file paths and commands into the sandbox."""
    for name, value in list(vars(module).items()):
        if not isinstance(value, str):
            continue
        for prefix, target in PATHS.items():
            if value.startswith(prefix):
                setattr(module, name, os.path.join(target, value[len(prefix):]))
    commands = {
        'PING_CMD': 'fake-ping',
        'WAKEONLAN_CMD': 'fake-wake',
        'UPSC_CMD': 'fake-upsc',
    }
    for name, script in commands.items():
        if hasattr(module, name):
            setattr(module, name, os.path.join(BIN_DIR, script))


# --- Pretend world -------------------------------------------------------

def read_config():
    config, hosts, current = {}, {}, None
    with open(CONFIG_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('[') and line.endswith(']'):
                current = line[1:-1] if line[1:-1].startswith('WAKE_HOST_') else '_skip'
                if current != '_skip':
                    hosts[current] = {}
                continue
            if '=' in line:
                key, value = line.split('=', 1)
                value = value.strip().strip('"\'').strip()
                if current is None:
                    config[key.strip()] = value
                elif current != '_skip':
                    hosts[current][key.strip()] = value
    return config, hosts


class World:
    """The pretend grid, machines and battery that the sandbox simulates."""

    def __init__(self):
        self.lock = threading.RLock()
        self.grid_on = True
        self.machines = {}      # ip -> state
        self.battery = {'soc': 96.0, 'voltage': 13.4, 'current': 0.8}
        self.events = collections.deque(maxlen=60)
        self.emails = collections.deque(maxlen=30)
        self._wake_offset = 0
        self.sync_machines()

    # bookkeeping

    def log(self, text):
        stamp = datetime.now().strftime('%H:%M:%S')
        with self.lock:
            self.events.appendleft(f"{stamp}  {text}")

    def record_email(self, subject, body):
        stamp = datetime.now().strftime('%H:%M:%S')
        with self.lock:
            self.emails.appendleft({'time': stamp, 'subject': subject, 'body': body})
        self.log(f"Email (not sent): {subject}")

    def sync_machines(self):
        """Pick up machines added, removed or edited in the sandbox settings."""
        config, hosts = read_config()
        with self.lock:
            seen = set()
            for section, params in hosts.items():
                ip = params.get('IP')
                if not ip:
                    continue
                seen.add(ip)
                machine = self.machines.setdefault(ip, {
                    'on': True, 'agent': True, 'wakes': True,
                    'boot_at': None, 'off_at': None, 'flag_since': None,
                    'next_config_at': 0, 'config': None,
                })
                machine['name'] = params.get('NAME', section)
                machine['mac'] = params.get('MAC', '').lower()
                machine['ups_client'] = 'SHUTDOWN_DELAY_MINUTES' in params
            for ip in [ip for ip in self.machines if ip not in seen]:
                del self.machines[ip]
            self.sentinels = config.get('SENTINEL_HOSTS', '').split()
            self.battery_enabled = config.get('BATTERY_ENABLED', 'false').lower() == 'true'

    def reachable(self):
        with self.lock:
            ips = list(self.sentinels) if self.grid_on else []
            ips += [ip for ip, m in self.machines.items() if m['on']]
        # The rest of the pretend home network (dev/fake_lan.py).
        ips += [ip for ip in fake_lan.answering(self.grid_on) if ip not in ips]
        return ips

    # actions

    def set_grid(self, on):
        with self.lock:
            if self.grid_on == on:
                return
            self.grid_on = on
            if not on:
                # Machines that are not UPS clients sit on the grid, not the UPS.
                for m in self.machines.values():
                    if not m['ups_client'] and m['on']:
                        m['on'] = False
                        m['boot_at'] = None
        self.log("Grid power restored" if on else "Grid power LOST - sentinels stop answering")

    def tick(self):
        """Advance the pretend world by one step (called every second)."""
        now = time.time()
        self.sync_machines()
        self._process_wake_requests(now)
        with self.lock:
            for ip, m in self.machines.items():
                if m['boot_at'] and now >= m['boot_at']:
                    m['boot_at'] = None
                    m['on'] = True
                    m['flag_since'] = None
                    self.log(f"{m['name']} finished booting")
                if m['off_at'] and now >= m['off_at']:
                    m['off_at'] = None
                    m['on'] = False
                    self.log(f"{m['name']} powered off")
            self._tick_battery()
        self._write_reachable()

    def _process_wake_requests(self, now):
        path = os.path.join(WORLD_DIR, 'wake_requests.log')
        try:
            with open(path) as f:
                f.seek(self._wake_offset)
                lines = f.readlines()
                self._wake_offset = f.tell()
        except FileNotFoundError:
            self._wake_offset = 0
            return
        for line in lines:
            parts = line.split()
            if len(parts) < 2:
                continue
            mac = parts[1].lower().replace('-', ':')
            with self.lock:
                target = next((m for m in self.machines.values() if m['mac'] == mac), None)
                if target is None:
                    self.log(f"Wake-on-LAN for unknown MAC {mac} - nothing woke up")
                elif target['on'] or target['boot_at']:
                    continue
                elif not target['wakes']:
                    self.log(f"Wake-on-LAN sent to {target['name']}, but it ignores it (simulated fault)")
                elif not self.grid_on and not target['ups_client']:
                    self.log(f"Wake-on-LAN sent to {target['name']}, but it has no power")
                else:
                    target['boot_at'] = now + BOOT_SECONDS
                    self.log(f"Wake-on-LAN received by {target['name']} - booting ({BOOT_SECONDS}s)")

    def _tick_battery(self):
        b = self.battery
        if self.grid_on:
            if b['soc'] < 100:
                b['soc'] = min(100.0, b['soc'] + 0.05)
                b['current'] = 6.0
                b['voltage'] = 14.1
            else:
                b['current'] = 0.4
                b['voltage'] = 13.5
        else:
            b['soc'] = max(0.0, b['soc'] - 0.08)
            b['current'] = -32.0
            b['voltage'] = round(11.2 + b['soc'] / 100 * 1.6, 2)

    def _write_reachable(self):
        path = os.path.join(WORLD_DIR, 'reachable.txt')
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            f.write('\n'.join(self.reachable()) + '\n')
        os.replace(tmp, path)

    def snapshot(self):
        with self.lock:
            return {
                'grid_on': self.grid_on,
                'battery_enabled': self.battery_enabled,
                'battery': dict(self.battery),
                'sentinels': list(self.sentinels),
                'machines': {ip: {k: v for k, v in m.items() if k != 'config'}
                             for ip, m in self.machines.items()},
                'events': list(self.events),
                'emails': list(self.emails),
            }


WORLD = None


# --- Pretend UPS_monitor agents -----------------------------------------

def api_call(method, path, payload=None):
    url = f"http://127.0.0.1:{API_PORT}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': f'Bearer {TOKEN}',
        'Content-Type': 'application/json',
    })
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode())


def agent_pass(ip, m):
    """One check of a pretend agent, mirroring ups_monitor.sh v4.4.0."""
    now = time.time()
    if now >= m['next_config_at'] or m['config'] is None:
        try:
            m['config'] = api_call('GET', f'/config?ip={ip}')
        except (urllib.error.URLError, OSError, ValueError):
            m['config'] = m['config'] or {'SHUTDOWN_DELAY_MINUTES': '5', 'IGNORE_SIMULATION': False}
        m['next_config_at'] = now + 60

    delay = int(m['config'].get('SHUTDOWN_DELAY_MINUTES', 5)) * 60
    ignore = bool(m['config'].get('IGNORE_SIMULATION'))

    status = api_call('GET', f'/upsc?ip={ip}')
    ups = status.get('ups', {})
    current, simulation = ups.get('status'), ups.get('simulation') is True

    def report(state, remaining=None, delay_min=None):
        api_call('POST', '/status', {'ip': ip, 'status': state,
                                     'remaining_seconds': remaining,
                                     'shutdown_delay': delay_min})

    if current == 'OB LB':
        if simulation and ignore:
            m['flag_since'] = None
            report('online')
        elif delay == 0 or (m['flag_since'] and now - m['flag_since'] >= delay):
            report('shutting_down')
            m['flag_since'] = None
            m['off_at'] = now + SHUTDOWN_SECONDS
            WORLD.log(f"{m['name']}: agent shutting the machine down")
        elif not m['flag_since']:
            m['flag_since'] = now
            report('shutdown_pending', delay, delay // 60)
            WORLD.log(f"{m['name']}: agent started a {delay // 60} min shutdown countdown")
        else:
            report('shutdown_pending', int(delay - (now - m['flag_since'])), delay // 60)
    elif current == 'OL':
        if m['flag_since']:
            WORLD.log(f"{m['name']}: agent cancelled its countdown, power is back")
        m['flag_since'] = None
        report('online')


def agents_loop():
    time.sleep(3)
    while True:
        with WORLD.lock:
            active = [(ip, m) for ip, m in WORLD.machines.items()
                      if m['ups_client'] and m['agent'] and m['on']
                      and not m['off_at'] and not m['boot_at']]
        for ip, m in active:
            try:
                agent_pass(ip, m)
            except (urllib.error.URLError, OSError, ValueError) as e:
                WORLD.log(f"{m['name']}: agent could not reach the API ({e})")
        time.sleep(AGENT_SECONDS)


def world_loop():
    while True:
        try:
            WORLD.tick()
        except Exception as e:  # keep the sandbox alive whatever happens
            logging.getLogger('sandbox').exception("world tick failed: %s", e)
        time.sleep(1)


# --- Power manager loop --------------------------------------------------

RUN_LOCK = threading.Lock()


def manager_loop(power_manager):
    iteration = 0
    while True:
        started = time.monotonic()
        with RUN_LOCK:
            try:
                power_manager.PowerManager().run(iteration=iteration % 4)
            except SystemExit:
                pass
            except Exception as e:
                logging.getLogger('sandbox').exception("power manager pass failed: %s", e)
        iteration += 1
        time.sleep(max(0.0, PASS_SECONDS - (time.monotonic() - started)))


# --- Fake battery monitor -------------------------------------------------

def make_vbm_app():
    from flask import Flask, jsonify
    vbm = Flask('fake_vbm')

    @vbm.route('/api/v1/status')
    def status():
        with WORLD.lock:
            b = dict(WORLD.battery)
            grid = WORLD.grid_on
        draw = -b['current'] if b['current'] < 0 else 0
        remaining = int((b['soc'] - 50) / 100 * 100 / draw * 60) if draw and b['soc'] > 50 else None
        return jsonify({
            'connected': True,
            'last_update': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'voltage': round(b['voltage'], 2), 'current': round(b['current'], 2),
            'power': round(b['voltage'] * b['current'], 1), 'soc': round(b['soc'], 1),
            'remaining_mins': remaining, 'temperature': 22.5,
            'ac_power': grid, 'ac_power_since': None,
        })

    @vbm.route('/api/v1/health')
    def health():
        return jsonify({'status': 'ok', 'ble': {'connected': True}})

    return vbm


# --- Control panel (/dev) -------------------------------------------------

def register_dev_routes(web_app, power_manager):
    from flask import Blueprint, jsonify, redirect, render_template_string, request, url_for

    dev = Blueprint('sandbox_dev', __name__)

    def set_setting(key, value):
        with RUN_LOCK:
            power_manager.save_setting_to_config(key, value)
        WORLD.sync_machines()

    def machine_by_name(fragment):
        return next((ip for ip, m in WORLD.machines.items()
                     if fragment.lower() in m['name'].lower()), None)

    def preset_normal():
        with RUN_LOCK:
            reset_runtime()
            WORLD._wake_offset = 0
            power_manager.save_setting_to_config('POWER_SIMULATION_MODE', 'false')
        with WORLD.lock:
            WORLD.grid_on = True
            for m in WORLD.machines.values():
                m.update(on=True, agent=True, wakes=True, boot_at=None,
                         off_at=None, flag_since=None)
        WORLD.log("Preset: everything normal (runtime state cleared)")

    actions = {
        'grid_off': lambda: WORLD.set_grid(False),
        'grid_on': lambda: WORLD.set_grid(True),
        'test_on': lambda: (set_setting('POWER_SIMULATION_MODE', 'true'), WORLD.log("Test mode switched ON")),
        'test_off': lambda: (set_setting('POWER_SIMULATION_MODE', 'false'), WORLD.log("Test mode switched OFF")),
        'battery_on': lambda: (set_setting('BATTERY_ENABLED', 'true'), WORLD.log("Battery monitor enabled")),
        'battery_off': lambda: (set_setting('BATTERY_ENABLED', 'false'), WORLD.log("Battery monitor disabled")),
        'normal': preset_normal,
    }

    @dev.route('/dev')
    def panel():
        return render_template_string(PANEL_HTML)

    @dev.route('/dev/state')
    def state():
        snap = WORLD.snapshot()
        config, _ = read_config()
        snap['test_mode'] = config.get('POWER_SIMULATION_MODE', 'false') == 'true'
        try:
            with open(os.path.join(PATHS['/var/run/nut/'], 'power_manager.state')) as f:
                snap['manager_state'] = f.read().split('\n')[0].replace('STATE=', '') or 'idle'
        except OSError:
            snap['manager_state'] = 'idle'
        try:
            with open(os.path.join(PATHS['/var/run/nut/'], 'virtual.device')) as f:
                snap['ups_status'] = f.read().strip().replace('ups.status: ', '')
        except OSError:
            snap['ups_status'] = '?'
        return jsonify(snap)

    @dev.route('/dev/action', methods=['POST'])
    def action():
        name = request.form.get('action', '')
        ip = request.form.get('ip')
        if name in actions:
            actions[name]()
        elif ip and ip in WORLD.machines and name in ('power', 'agent', 'wakes'):
            with WORLD.lock:
                m = WORLD.machines[ip]
                if name == 'power':
                    m['on'] = not m['on']
                    m['boot_at'] = m['off_at'] = None
                    WORLD.log(f"{m['name']} switched {'on' if m['on'] else 'off'} by hand")
                elif name == 'agent':
                    m['agent'] = not m['agent']
                    WORLD.log(f"{m['name']}: agent {'installed' if m['agent'] else 'removed'}")
                else:
                    m['wakes'] = not m['wakes']
                    WORLD.log(f"{m['name']}: {'responds to' if m['wakes'] else 'ignores'} Wake-on-LAN")
        if request.headers.get('X-Requested-With') == 'fetch':
            return jsonify({'ok': True})
        return redirect(url_for('sandbox_dev.panel'))

    web_app.register_blueprint(dev)


PANEL_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sandbox control panel</title>
<style>
 body{font:14px/1.45 -apple-system,system-ui,sans-serif;margin:0;background:#fafafa;color:#18181b}
 header{background:#18181b;color:#fff;padding:12px 20px;display:flex;gap:16px;align-items:center}
 header a{color:#a1a1aa} main{max-width:1100px;margin:0 auto;padding:20px;display:grid;gap:16px;grid-template-columns:1fr 1fr}
 section{background:#fff;border:1px solid #e4e4e7;border-radius:10px;padding:16px} h2{font-size:15px;margin:0 0 12px}
 button{font:inherit;padding:6px 12px;border-radius:6px;border:1px solid #d4d4d8;background:#fff;cursor:pointer;margin:2px}
 button.primary{background:#18181b;color:#fff;border-color:#18181b} button.danger{background:#dc2626;color:#fff;border-color:#dc2626}
 table{width:100%;border-collapse:collapse} td,th{text-align:left;padding:6px 4px;border-bottom:1px solid #f4f4f5}
 .pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;background:#f4f4f5}
 .on{background:#dcfce7;color:#166534}.off{background:#fee2e2;color:#991b1b}.wait{background:#fef3c7;color:#92400e}
 pre{white-space:pre-wrap;margin:0;font:12px/1.5 ui-monospace,monospace;max-height:320px;overflow:auto}
 .full{grid-column:1/-1} @media(max-width:800px){main{grid-template-columns:1fr}}
</style></head><body>
<header><strong>Sandbox control panel</strong><span>Pretend world only &mdash; nothing here touches a real machine.</span>
<a href="/" target="_blank">Open the app &rarr;</a></header>
<main>
 <section><h2>Scenarios</h2>
  <button class="primary" data-a="normal">Everything normal</button>
  <button class="danger" data-a="grid_off">Power cut</button>
  <button data-a="grid_on">Power back</button>
  <button data-a="test_on">Test mode on</button><button data-a="test_off">Test mode off</button>
  <button data-a="battery_on">Battery monitor on</button><button data-a="battery_off">Battery monitor off</button>
  <p id="summary"></p></section>
 <section><h2>Pretend battery</h2><div id="battery"></div></section>
 <section class="full"><h2>Pretend machines</h2>
  <table><thead><tr><th>Machine</th><th>Address</th><th>Power</th><th>Agent</th><th>Wake-on-LAN</th><th></th></tr></thead>
  <tbody id="machines"></tbody></table></section>
 <section><h2>What happened</h2><pre id="events"></pre></section>
 <section><h2>Emails the app would have sent</h2><pre id="emails"></pre></section>
</main>
<script>
async function act(a, ip){const f=new FormData();f.append('action',a);if(ip)f.append('ip',ip);
 await fetch('/dev/action',{method:'POST',body:f,headers:{'X-Requested-With':'fetch'}});refresh();}
document.querySelectorAll('[data-a]').forEach(b=>b.onclick=()=>act(b.dataset.a));
function pill(ok,yes,no,wait){return `<span class="pill ${wait?'wait':ok?'on':'off'}">${wait||(ok?yes:no)}</span>`}
async function refresh(){const s=await (await fetch('/dev/state')).json();
 document.getElementById('summary').innerHTML=`Grid: ${pill(s.grid_on,'on','OFF')} &nbsp; Test mode: ${pill(!s.test_mode,'off','ON')} &nbsp; Power manager: <b>${s.manager_state}</b> &nbsp; UPS says: <b>${s.ups_status}</b>`;
 const b=s.battery;document.getElementById('battery').innerHTML=s.battery_enabled?`SoC <b>${b.soc.toFixed(1)}%</b> &middot; ${b.voltage.toFixed(2)} V &middot; ${b.current.toFixed(1)} A`:'Battery monitor is disabled in the sandbox settings.';
 document.getElementById('machines').innerHTML=Object.entries(s.machines).map(([ip,m])=>`<tr><td>${m.name}${m.ups_client?'':' <span class=pill>WoL only</span>'}</td><td>${ip}</td>
 <td>${pill(m.on,'on','off',m.boot_at?'booting':m.off_at?'shutting down':'')}</td><td>${m.ups_client?pill(m.agent,'running','none'):'&ndash;'}</td><td>${pill(m.wakes,'works','ignored')}</td>
 <td><button onclick="act('power','${ip}')">Power</button>${m.ups_client?`<button onclick="act('agent','${ip}')">Agent</button>`:''}<button onclick="act('wakes','${ip}')">WoL fault</button></td></tr>`).join('');
 document.getElementById('events').textContent=s.events.join('\\n')||'Nothing yet.';
 document.getElementById('emails').textContent=s.emails.map(e=>`${e.time}  ${e.subject}\\n${e.body}\\n`).join('\\n')||'None yet.';}
refresh();setInterval(refresh,2000);
</script></body></html>"""


# --- Wiring ---------------------------------------------------------------

def serve(app, port, name):
    from werkzeug.serving import make_server
    server = make_server('127.0.0.1', port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, name=name, daemon=True)
    thread.start()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--reset', action='store_true',
                        help='start again from dev/test_config.conf')
    parser.add_argument('--first-run', action='store_true',
                        help='start as a brand-new install: no password, no sentinels')
    args = parser.parse_args()

    prepare_sandbox(args.reset, args.first_run)
    sys.path.insert(0, APP_DIR)

    import api
    remap(api)
    api.API_TOKEN = api.load_api_token()

    import power_manager
    remap(power_manager)
    def sandbox_logging(debug_mode=False):
        logger = logging.getLogger(power_manager.APP_NAME)
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG if debug_mode else logging.INFO)
        handler = logging.FileHandler(power_manager.LOG_FILE)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(handler)
        return logger

    power_manager.log = sandbox_logging()
    power_manager.setup_logging = sandbox_logging

    import web_auth
    remap(web_auth)
    # The sandbox control panel stays reachable without signing in (it only
    # listens on 127.0.0.1 and changes nothing but the pretend world).
    web_auth.PUBLIC_PREFIXES.append('sandbox_dev.')

    import web_gui
    remap(web_gui)
    # Pick up template edits without restarting the sandbox.
    web_gui.app.config['TEMPLATES_AUTO_RELOAD'] = True
    web_gui.app.jinja_env.auto_reload = True

    global WORLD
    WORLD = World()

    import network_scan
    remap(network_scan)
    fake_lan.install(network_scan, WORLD.reachable,
                     lambda: {h['IP']: h.get('MAC', '').lower() for h in read_config()[1].values() if h.get('IP')})
    power_manager.Notifier._send_email = lambda self, subject, body: WORLD.record_email(subject, body)
    web_gui.send_email = lambda subject, body, config: (
        WORLD.record_email(subject, body) or True,
        "Sandbox: the test email was recorded in the control panel, not sent.")

    register_dev_routes(web_gui.app, power_manager)
    try:
        import dev_styleguide  # noqa: F401  (added in a later step, optional)
    except ImportError:
        pass
    else:
        dev_styleguide.register(web_gui.app)

    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    for target, name in ((world_loop, 'world'), (manager_loop, 'manager'),
                         (agents_loop, 'agents')):
        args_ = (power_manager,) if target is manager_loop else ()
        threading.Thread(target=target, args=args_, name=name, daemon=True).start()

    serve(make_vbm_app(), VBM_PORT, 'vbm')
    serve(api.app, API_PORT, 'api')
    print(f"Sandbox folder : {SANDBOX}")
    print(f"Web GUI        : http://127.0.0.1:{WEB_PORT}/")
    print(f"Control panel  : http://127.0.0.1:{WEB_PORT}/dev")
    print(f"API            : http://127.0.0.1:{API_PORT}/ (token: {TOKEN})")
    print(f"Web sign-in    : password '{SANDBOX_PASSWORD}' (sandbox only)")
    sys.stdout.flush()

    from werkzeug.serving import make_server
    make_server('127.0.0.1', WEB_PORT, web_gui.app, threaded=True).serve_forever()


if __name__ == '__main__':
    sys.path.insert(0, DEV_DIR)
    try:
        main()
    except KeyboardInterrupt:
        pass
