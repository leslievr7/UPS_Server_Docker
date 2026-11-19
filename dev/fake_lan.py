"""A pretend home network for the sentinel picker in the sandbox.

network_scan normally pings real addresses and reads the kernel's neighbour
table. In the sandbox its backend is swapped for this module, which only
answers from the list below. Nothing is sent on the network.

Addresses are in 192.0.2.0/24 (TEST-NET-1, never routed). The MAC prefixes are
real makers' prefixes, so the picker can show makers; the rest of each MAC is
made up. Phones use "private" MACs, as real phones do.
"""

import threading
import time

SERVER_IP = '192.0.2.2'
GATEWAY_IP = '192.0.2.1'

# ip: (mac, reverse-DNS name, answers ping, keeps running in a power cut)
# The router and this server are on the UPS; phones and laptops have batteries.
DEVICES = {
    '192.0.2.1': ('00:0a:eb:10:00:01', 'openwrt', True, True),
    '192.0.2.2': ('28:cd:c1:10:00:02', 'ups-server', True, True),
    '192.0.2.20': ('00:4b:12:10:00:20', 'ESP_3A1B2C', True, False),
    '192.0.2.21': ('84:00:ec:10:00:21', 'shellyplug-s-kitchen', True, False),
    '192.0.2.22': ('00:17:88:10:00:22', 'hue-bridge', True, False),
    '192.0.2.23': ('00:0d:4b:10:00:23', 'roku-livingroom', True, False),
    '192.0.2.24': ('00:04:3c:10:00:24', 'Sonos-Bedroom', True, False),
    '192.0.2.25': ('00:71:47:10:00:25', '', True, False),
    '192.0.2.26': ('00:00:48:10:00:26', 'EPSON-ET2850', False, False),
    '192.0.2.27': ('00:11:32:10:00:27', 'diskstation', True, False),
    '192.0.2.28': ('00:1c:62:10:00:28', 'LG-webOS-TV', True, False),
    '192.0.2.40': ('da:7e:91:10:00:40', 'iPhone', True, True),
    '192.0.2.41': ('6a:0c:11:10:00:41', 'Galaxy-S24', True, True),
    '192.0.2.42': ('00:03:93:10:00:42', 'macbook-air', True, True),
    '192.0.2.50': ('00:1b:a9:10:00:50', '', True, False),
    # The sandbox config's sentinels. They exist even in --first-run mode,
    # before anything watches them.
    '192.0.2.101': ('00:4b:12:10:01:01', 'esp-plug-hall', True, False),
    '192.0.2.102': ('00:4b:12:10:01:02', 'esp-plug-office', True, False),
    '192.0.2.103': ('00:0d:4b:10:01:03', 'roku-bedroom', True, False),
    '192.0.2.104': ('00:04:3c:10:01:04', 'Sonos-Kitchen', True, False),
    '192.0.2.105': ('84:00:ec:10:01:05', 'shelly-garage', True, False),
}


def answering(grid_on):
    """The pretend devices that answer pings right now."""
    return [ip for ip, (_, _, pings, on_ups) in DEVICES.items() if pings and (grid_on or on_ups)]


def install(network_scan, reachable, machine_macs=lambda: {}):
    """Point network_scan at the pretend network.

    reachable() lists every pretend IP that answers pings right now: the
    world's machines plus answering(); the power manager pings those all the
    time.
    machine_macs() gives {ip: mac} for the pretend protected machines.
    """
    probed = set()
    lock = threading.Lock()

    def answers(ip):
        return ip in set(reachable())

    def mac_of(ip):
        if ip in DEVICES:
            return DEVICES[ip][0]
        return machine_macs().get(ip)

    def ping_sweep(ips, wait, on_sent, on_reply):
        answered = set()
        for start in range(0, len(ips), 8):
            batch = ips[start:start + 8]
            with lock:
                probed.update(batch)
            time.sleep(0.06)
            on_sent(min(start + 8, len(ips)))
            for ip in batch:
                if answers(ip):
                    answered.add(ip)
                    on_reply(ip)
        time.sleep(min(wait, 0.8))
        return answered

    def arp_table():
        with lock:
            seen = set(probed)
        # The power manager pings the sentinels all the time, so they're known too.
        seen |= set(reachable())
        table = {}
        for ip in seen:
            mac = mac_of(ip)
            if mac and (answers(ip) or (ip in DEVICES and not DEVICES[ip][2])):
                table[ip] = mac
        return table

    def reverse_name(ip):
        time.sleep(0.15)
        if ip in DEVICES:
            return DEVICES[ip][1]
        return ''  # the protected machines show their names from the settings

    network_scan.backend.update({
        'server_ip': lambda: SERVER_IP,
        'gateway_ip': lambda: GATEWAY_IP,
        'ping_sweep': ping_sweep,
        'arp_table': arp_table,
        'reverse_name': reverse_name,
    })
