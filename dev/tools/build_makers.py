#!/usr/bin/env python3
"""
Build app/data/oui_makers.tsv: which maker owns a MAC address prefix, for the
makers commonly found on a home network, plus a hint of what kind of device
they make. The device picker uses it to say "Espressif (smart home)" rather
than "Unknown device".

The full IEEE registry is about 4 MB; only the makers below are kept. Makers
whose prefixes are mostly telecom or office gear (Cisco, Huawei, Nokia...)
are left out to keep the file small.

Usage:
    curl -sSfLO https://standards-oui.ieee.org/oui/oui.csv
    python3 dev/tools/build_makers.py oui.csv

Output lines: PREFIX<TAB>Maker<TAB>kind, e.g. "240AC4	Espressif	smart".
kind is one of: smart light tv speaker printer camera console network computer
or empty (the maker makes too many kinds of device to guess).
"""

import csv
import os
import sys

# (text found in the IEEE organisation name, short name shown, kind)
# First match wins, so put specific names before general ones.
MAKERS = [
    # smart-home chips and brands: nearly always wall-powered and always on
    ('espressif', 'Espressif', 'smart'),
    ('tuya', 'Tuya', 'smart'),
    ('allterco', 'Shelly', 'smart'),
    ('shelly', 'Shelly', 'smart'),
    ('itead', 'Sonoff', 'smart'),
    ('meross', 'Meross', 'smart'),
    ('belkin', 'Belkin', 'smart'),
    ('ecobee', 'ecobee', 'smart'),
    ('tado', 'tado', 'smart'),
    ('netatmo', 'Netatmo', 'smart'),
    ('nanoleaf', 'Nanoleaf', 'light'),
    ('signify', 'Philips Hue', 'light'),
    ('philips lighting', 'Philips Hue', 'light'),
    ('lifx', 'LIFX', 'light'),
    ('wiz connected', 'WiZ', 'light'),
    ('yeelight', 'Yeelight', 'light'),
    ('govee', 'Govee', 'light'),
    # TVs and streaming boxes
    ('roku', 'Roku', 'tv'),
    ('tp vision', 'Philips TV', 'tv'),
    ('hisense', 'Hisense', 'tv'),
    ('tcl ', 'TCL', 'tv'),
    ('vizio', 'Vizio', 'tv'),
    ('lg electronics', 'LG', 'tv'),
    ('sony interactive', 'PlayStation', 'console'),
    ('sony', 'Sony', 'tv'),
    ('panasonic', 'Panasonic', 'tv'),
    ('nvidia', 'NVIDIA', 'tv'),
    # speakers
    ('sonos', 'Sonos', 'speaker'),
    ('bose', 'Bose', 'speaker'),
    ('amazon technologies', 'Amazon', 'speaker'),
    # printers
    ('seiko epson', 'Epson', 'printer'),
    ('brother industries', 'Brother', 'printer'),
    ('canon', 'Canon', 'printer'),
    ('hp inc', 'HP', 'printer'),
    ('hewlett packard', 'HP', 'printer'),
    ('kyocera', 'Kyocera', 'printer'),
    ('lexmark', 'Lexmark', 'printer'),
    ('xerox', 'Xerox', 'printer'),
    ('ricoh', 'Ricoh', 'printer'),
    # cameras and doorbells
    ('hikvision', 'Hikvision', 'camera'),
    ('dahua', 'Dahua', 'camera'),
    ('reolink', 'Reolink', 'camera'),
    ('wyze', 'Wyze', 'camera'),
    ('ezviz', 'EZVIZ', 'camera'),
    ('ring llc', 'Ring', 'camera'),
    ('axis communications', 'Axis', 'camera'),
    # consoles
    ('nintendo', 'Nintendo', 'console'),
    ('microsoft', 'Microsoft', ''),
    # network gear
    ('ubiquiti', 'Ubiquiti', 'network'),
    ('netgear', 'Netgear', 'network'),
    ('asustek', 'ASUS', 'network'),
    ('routerboard', 'MikroTik', 'network'),
    ('mikrotik', 'MikroTik', 'network'),
    ('tp-link', 'TP-Link', 'network'),
    ('d-link', 'D-Link', 'network'),
    ('zyxel', 'Zyxel', 'network'),
    ('linksys', 'Linksys', 'network'),
    ('avm ', 'AVM', 'network'),
    # computers and storage
    ('raspberry pi', 'Raspberry Pi', 'computer'),
    ('synology', 'Synology', 'computer'),
    ('qnap', 'QNAP', 'computer'),
    ('western digital', 'Western Digital', 'computer'),
    ('intel corporate', 'Intel', 'computer'),
    ('dell ', 'Dell', 'computer'),
    ('lenovo', 'Lenovo', 'computer'),
    ('super micro', 'Supermicro', 'computer'),
    ('giga-byte', 'Gigabyte', 'computer'),
    ('micro-star', 'MSI', 'computer'),
    # makers of many kinds of device: name only
    ('apple', 'Apple', ''),
    ('samsung', 'Samsung', ''),
    ('google', 'Google', ''),
    ('xiaomi', 'Xiaomi', ''),
    ('oneplus', 'OnePlus', ''),
    ('oppo', 'OPPO', ''),
    ('vivo mobile', 'vivo', ''),
    ('realme', 'realme', ''),
    ('honor device', 'Honor', ''),
]

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTPUT = os.path.join(REPO, 'app', 'data', 'oui_makers.tsv')


def main(source):
    rows = []
    with open(source, newline='', encoding='utf-8') as f:
        for record in csv.DictReader(f):
            if record['Registry'] != 'MA-L':
                continue
            org = record['Organization Name'].lower()
            for needle, name, kind in MAKERS:
                if needle in org:
                    rows.append((record['Assignment'].upper(), name, kind))
                    break
    rows.sort()
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, 'w') as f:
        f.write('# MAC prefix, maker, kind. Built by dev/tools/build_makers.py from the IEEE registry.\n')
        for row in rows:
            f.write('\t'.join(row) + '\n')
    print(f'Wrote {len(rows)} prefixes to {OUTPUT} ({os.path.getsize(OUTPUT)} bytes)')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
