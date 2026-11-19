#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
UPS Server Web GUI
Author: MarekWo, Claude
Description: A web interface for managing UPS Server configuration with unified power_manager.conf
"""

import os
import sys
import subprocess
import ipaddress
import json
import smtplib
import re
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import formataddr
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, make_response, g, has_request_context
from werkzeug.exceptions import BadRequest


# Add the current directory to Python path to import modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api import API_TOKEN, get_ups_name, get_server_ip, read_power_state
from battery_monitor import BatteryMonitor
from dashboard_view import build_dashboard
import config_store
import reachability
import web_auth
import web_onboarding
import web_settings
import network_scan

# Import version information
try:
    from version_info import get_version_info, get_version_string
except ImportError:
    # Fallback if version_info module is not available
    def get_version_info():
        return {
            "version_string": "development",
            "commit_hash": "unknown",
            "source": "fallback"
        }
    def get_version_string():
        return "development"

app = Flask(__name__)
# Sign-in, the session secret and CSRF protection live in web_auth.py.
web_auth.init_app(app)


@app.context_processor
def inject_version_info():
    """Every page shows the version in its footer."""
    return {'version_info': get_version_info()}

# --- Configuration ---
POWER_MANAGER_CONFIG = "/etc/nut/power_manager.conf"
PING_CMD = "/bin/ping"
WAKEONLAN_CMD = "/usr/bin/wakeonlan"
CLIENT_STATUS_FILE = "/var/run/nut/client_status.json"
MANAGER_STATE_FILE = "/var/run/nut/power_manager.state"

# --- Helper Functions ---

def read_manager_state():
    """The power manager's current episode (STATE, TIMESTAMP, ...), or {}."""
    state = {}
    try:
        with open(MANAGER_STATE_FILE, 'r') as f:
            for line in f:
                if '=' in line:
                    key, value = line.strip().split('=', 1)
                    state[key] = value
    except (IOError, OSError):
        pass
    return state


def collect_dashboard():
    """Gather the state files and turn them into the dashboard view."""
    pm_config, wake_hosts, schedules = read_power_manager_config()
    power_state = read_power_state()
    recorded_sentinels, recorded_hosts = get_reachability()

    sentinel_ips = [ip.strip('"\'') for ip in pm_config.get('SENTINEL_HOSTS', '').split()]
    host_ips = [p.get('IP') for p in wake_hosts.values() if p.get('IP')]
    sentinels = dict(recorded_sentinels or {})
    hosts = dict(recorded_hosts or {})
    # Only addresses the power manager has not checked are pinged here, all at
    # once, so even a cold start costs about one ping timeout.
    missing = [ip for ip in sentinel_ips if ip and ip not in sentinels and validate_ip(ip)]
    missing += [ip for ip in host_ips if ip not in hosts]
    if missing:
        live = reachability.check(missing, ping_cmd=PING_CMD)
        for ip, up in live.items():
            if ip in sentinel_ips:
                sentinels[ip] = up
            if ip in host_ips:
                hosts[ip] = up

    view = build_dashboard(pm_config, wake_hosts, schedules, power_state,
                           get_client_statuses(), read_manager_state(), sentinels, hosts)
    try:
        view['address_changes'] = network_scan.address_changes(pm_config)
    except Exception as e:  # never let this extra check break the dashboard
        app.logger.warning(f"Could not check sentinel addresses: {e}")
        view['address_changes'] = []
    return view

def get_battery_section():
    """Battery data from the power manager snapshot, shaped for the dashboard.

    Always returns a dict, so templates and JS never have to null-check.
    """
    state = read_power_state()
    if state is None:
        return {
            'enabled': False,
            'available': False,
            'error': 'No fresh power state available from the power manager.',
        }

    battery = dict(state.get('battery', {}))
    battery['updated_at'] = state.get('updated_at')
    return battery

def get_reachability():
    """Who answered a ping, as last recorded by the power manager.

    Returns (sentinels, hosts), each {ip: bool}, or (None, None) when there is
    no fresh snapshot - read_power_state() discards anything over 90 s old -
    in which case callers ping by themselves, as the dashboard always used to.
    """
    state = read_power_state()
    reach = (state or {}).get('reachability') or {}
    if not reach.get('checked_at'):
        return None, None
    return reach.get('sentinels') or {}, reach.get('hosts') or {}


def is_reachable(ip, recorded):
    """Recorded ping result for ip, falling back to a live ping if unknown."""
    if recorded is not None and ip in recorded:
        return bool(recorded[ip])
    return ping_host(ip)


def get_client_statuses():
    """Reads the client status file."""
    if not os.path.exists(CLIENT_STATUS_FILE):
        return {}
    try:
        with open(CLIENT_STATUS_FILE, 'r') as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return {}

def read_power_manager_config():
    """Read power_manager.conf: (main config, wake hosts, schedules).

    Inside a request, what was read is remembered, so a later
    write_power_manager_config() in the same request applies only the changes
    made since (see config_store).
    """
    parsed = config_store.read(POWER_MANAGER_CONFIG)
    if has_request_context():
        g.config_base = config_store.snapshot(parsed)
    return parsed


# Settings the old writer left out when empty; keep doing that.
DROP_WHEN_EMPTY = (
    'SMTP_SERVER', 'SMTP_PORT', 'SMTP_USE_TLS', 'SMTP_USER', 'SMTP_PASSWORD',
    'SMTP_SENDER_NAME', 'SMTP_SENDER_EMAIL', 'SMTP_RECIPIENTS',
    'BATTERY_SIMULATION', 'BATTERY_SIM_AC_POWER', 'BATTERY_SIM_SOC',
    'BATTERY_SIM_VOLTAGE', 'BATTERY_SIM_CURRENT', 'BATTERY_SIM_REMAINING',
)


def write_power_manager_config(config, wake_hosts, schedules):
    """Save changes to power_manager.conf.

    Only settings that differ from what this request read are written; anything
    the power manager (or another browser tab) changed meanwhile, comments, and
    settings this page doesn't know about are kept.
    """
    config = {k: v for k, v in config.items() if not (k in DROP_WHEN_EMPTY and not v)}
    new = (config, wake_hosts, schedules)
    base = g.get('config_base') if has_request_context() else None
    config_store.save(POWER_MANAGER_CONFIG, new, base=base)
    if has_request_context():
        g.config_base = config_store.snapshot(new)

def ping_host(ip):
    """Check if host is online"""
    try:
        result = subprocess.run(
            [PING_CMD, "-c", "1", "-W", "1", ip],
            capture_output=True,
            timeout=2
        )
        return result.returncode == 0
    except:
        return False

def send_wol(mac, broadcast_ip):
    """Send Wake-on-LAN packet"""
    try:
        subprocess.run([WAKEONLAN_CMD, "-i", broadcast_ip, mac], 
                      capture_output=True, check=True)
        return True
    except:
        return False

def send_email(subject, body, config):
    """Send an email using configured SMTP settings."""
    try:
        # Extract SMTP configuration
        smtp_server = config.get('SMTP_SERVER')
        smtp_port_str = config.get('SMTP_PORT')
        smtp_user = config.get('SMTP_USER')
        smtp_password = config.get('SMTP_PASSWORD')
        smtp_sender_name = config.get('SMTP_SENDER_NAME')
        smtp_sender_email = config.get('SMTP_SENDER_EMAIL')
        smtp_recipients_str = config.get('SMTP_RECIPIENTS')
        smtp_use_tls = config.get('SMTP_USE_TLS', 'auto').lower()  # New option

        if not all([smtp_server, smtp_port_str, smtp_sender_email, smtp_recipients_str]):
            raise ValueError("SMTP server, port, sender email, and recipients must be configured.")

        smtp_port = int(smtp_port_str)
        smtp_recipients = [email.strip() for email in smtp_recipients_str.split(',') if email.strip()]

        # Create message
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From'] = formataddr((smtp_sender_name, smtp_sender_email))
        msg['To'] = ', '.join(smtp_recipients)

        # Send email
        server = None
        if smtp_port == 465:
            # Port 465 always uses SSL/TLS
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=10)
        else:
            # For other ports, determine STARTTLS usage based on configuration
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
            
            # Determine whether to use STARTTLS
            should_use_starttls = False
            if smtp_use_tls == 'true':
                should_use_starttls = True
            elif smtp_use_tls == 'false':
                should_use_starttls = False
            elif smtp_use_tls == 'auto':
                # Auto mode: use legacy logic (don't use STARTTLS on port 26)
                should_use_starttls = smtp_port != 26
            
            if should_use_starttls:
                server.starttls()
        
        if smtp_user and smtp_password:
            server.login(smtp_user, smtp_password)
        
        server.sendmail(smtp_sender_email, smtp_recipients, msg.as_string())
        server.quit()
        return True, "Email sent successfully."
    except Exception as e:
        app.logger.error(f"Failed to send email: {str(e)}")
        return False, str(e)


def validate_ip(ip):
    """Validate IP address"""
    try:
        ipaddress.ip_address(ip)
        return True
    except:
        return False

def validate_mac(mac):
    """Validate MAC address"""
    mac_pattern = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$')
    return bool(mac_pattern.match(mac))

def validate_email_list(emails_str):
    """Validate a comma-separated list of emails."""
    if not emails_str:
        return True # Empty is considered valid.
    
    email_pattern = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
    emails = [email.strip() for email in emails_str.split(',')]
    
    for email in emails:
        if email and not email_pattern.match(email):
            return False
    return True

def get_ups_clients_from_wake_hosts(wake_hosts):
    """Extract UPS clients (hosts with SHUTDOWN_DELAY_MINUTES) from wake hosts"""
    ups_clients = {}
    for section, params in wake_hosts.items():
        if 'SHUTDOWN_DELAY_MINUTES' in params and 'IP' in params:
            ups_clients[section] = {
                'ip': params['IP'],
                'name': params.get('NAME', section),
                'shutdown_delay': params['SHUTDOWN_DELAY_MINUTES']
            }
    return ups_clients

# --- Routes ---

@app.route('/')
def index():
    """Main dashboard"""
    try:
        view = collect_dashboard()
    except Exception as e:
        app.logger.error(f"Error building dashboard: {e}", exc_info=True)
        flash(f'Error loading the dashboard: {str(e)}', 'error')
        view = None
    return render_template('dashboard.html', view=view)


@app.route('/dashboard/live')
def dashboard_live():
    """The live part of the dashboard, re-rendered for the page to swap in."""
    try:
        view = collect_dashboard()
    except Exception as e:
        app.logger.error(f"Error building dashboard: {e}", exc_info=True)
        return f"Error: {e}", 500
    response = make_response(render_template('_dashboard_live.html', view=view))
    # The check time travels in headers so the HTML only changes when something
    # visible changes, and the page can skip redrawing (which would reset focus).
    response.headers['X-Checked-At'] = view.get('checked_local') or ''
    response.headers['X-Watch-Stale'] = '1' if view.get('watch_stale') else '0'
    return response

@app.route('/config')
def config():
    """Configuration page"""
    try:
        pm_config, wake_hosts, schedules = read_power_manager_config()
        ups_clients = get_ups_clients_from_wake_hosts(wake_hosts)
        # The email password never goes to the browser; the page only needs
        # to know whether one is saved.
        smtp_password_set = bool(pm_config.get('SMTP_PASSWORD'))
        page_config = {k: v for k, v in pm_config.items() if k != 'SMTP_PASSWORD'}
        # Machines that would act on test mode (protected and not ignoring it).
        test_mode_acting = [h.get('NAME', s) for s, h in sorted(wake_hosts.items())
                            if 'SHUTDOWN_DELAY_MINUTES' in h
                            and h.get('IGNORE_SIMULATION', 'false') != 'true']

        return render_template('config.html',
                             pm_config=page_config,
                             smtp_password_set=smtp_password_set,
                             test_mode_acting=test_mode_acting,
                             wake_hosts=wake_hosts,
                             ups_clients=ups_clients,
                             schedules=schedules,
                             schedules_past=[k for k, s in schedules.items() if one_time_is_past(s)],
                             version_info=get_version_info())
    except Exception as e:
        app.logger.error(f"Error in config route: {str(e)}", exc_info=True)
        flash(f'Error loading configuration: {str(e)}', 'error')
        return f"Error: {str(e)}", 500

# --- New Version Endpoint ---
@app.route('/version')
def version_endpoint():
    """API endpoint to get version information"""
    return jsonify(get_version_info())

# Settings saves, one per tab, live in web_settings.py.
web_settings.register(app, sys.modules[__name__])
web_onboarding.register(app, sys.modules[__name__])

@app.route('/test_smtp', methods=['POST'])
def test_smtp():
    """Send a test email."""
    try:
        pm_config, _, _ = read_power_manager_config()
        
        subject = "[UPS] Test Email from UPS Power Management Server"
        body = "This is a test email to verify that your SMTP configuration is correct.\n\n"
        body += "If you received this, notifications are working."
        
        success, message = send_email(subject, body, pm_config)
        
        if success:
            flash(f'Test email sent to {pm_config.get("SMTP_RECIPIENTS")}.', 'success')
        else:
            flash(f"Couldn't send the test email: {message}", 'error')
            
    except Exception as e:
        flash(f'An unexpected error occurred: {str(e)}', 'error')
        
    return redirect(url_for('config') + '#notifications')


def apply_battery_host_fields(host):
    """Copy the per-host battery settings from the submitted form into `host`.

    An empty field means "inherit the global default", so the key is removed
    rather than stored as an empty string - otherwise the evaluator would have
    to treat '' as a missing threshold everywhere.

    The battery fields are only rendered while the integration is enabled. When
    they are absent, this is a no-op: an unrendered field is not the same as a
    cleared one, and editing a host's name with the integration temporarily off
    must not silently wipe its thresholds.
    """
    if 'battery_fields_present' not in request.form:
        return

    source = request.form.get('power_source', 'sentinel').strip().lower()
    if source in ('battery', 'both'):
        host['POWER_SOURCE'] = source
    else:
        host.pop('POWER_SOURCE', None)  # sentinel is the default; keep the file clean

    for field, key in (
        ('shutdown_soc', 'SHUTDOWN_SOC'),
        ('shutdown_voltage', 'SHUTDOWN_VOLTAGE'),
        ('critical_voltage', 'CRITICAL_VOLTAGE'),
        ('min_runtime_minutes', 'MIN_RUNTIME_MINUTES'),
        ('wol_min_soc', 'WOL_MIN_SOC'),
    ):
        value = request.form.get(field, '').strip()
        if value:
            host[key] = value
        else:
            host.pop(key, None)

@app.route('/add_wake_host', methods=['POST'])
def add_wake_host():
    """Add new wake host"""
    try:
        pm_config, wake_hosts, schedules = read_power_manager_config()

        # Find next available wake host number
        existing_numbers = [int(s.replace('WAKE_HOST_', '')) for s in wake_hosts.keys() if s.startswith('WAKE_HOST_')]
        next_num = max(existing_numbers) + 1 if existing_numbers else 1
        section_name = f"WAKE_HOST_{next_num}"

        # Get form data and validate
        name = request.form.get('name', '').strip()
        ip = request.form.get('ip', '').strip()
        mac = request.form.get('mac', '').strip()
        broadcast_ip = request.form.get('broadcast_ip', '').strip()
        shutdown_delay = request.form.get('shutdown_delay', '').strip()
        auto_wol = 'true' if 'auto_wol' in request.form else 'false'
        ignore_simulation = 'true' if 'ignore_simulation' in request.form else 'false'
        
        if not all([name, ip, mac]):
            flash('Name, IP address and MAC address are all needed.', 'error')
            return redirect(url_for('config') + '#machines')
        if not validate_ip(ip):
            flash(f'Invalid IP address: {ip}', 'error')
            return redirect(url_for('config') + '#machines')
        if not validate_mac(mac):
            flash(f'Invalid MAC address: {mac}', 'error')
            return redirect(url_for('config') + '#machines')
        if broadcast_ip and not validate_ip(broadcast_ip):
            flash(f'Invalid broadcast IP address: {broadcast_ip}', 'error')
            return redirect(url_for('config') + '#machines')
        
        # Add new wake host
        wake_hosts[section_name] = {'NAME': name, 'IP': ip, 'MAC': mac, 'AUTO_WOL': auto_wol, 'IGNORE_SIMULATION': ignore_simulation}
        if broadcast_ip:
            wake_hosts[section_name]['BROADCAST_IP'] = broadcast_ip
        if shutdown_delay:
            wake_hosts[section_name]['SHUTDOWN_DELAY_MINUTES'] = shutdown_delay
        apply_battery_host_fields(wake_hosts[section_name])

        write_power_manager_config(pm_config, wake_hosts, schedules)
        flash(f'Added {name}.', 'success')
        
    except Exception as e:
        flash(f'Error adding host: {str(e)}', 'error')
    
    return redirect(url_for('config') + '#machines')

@app.route('/edit_wake_host/<section>', methods=['POST'])
def edit_wake_host(section):
    """Edit existing wake host"""
    try:
        pm_config, wake_hosts, schedules = read_power_manager_config()
        
        if section not in wake_hosts:
            flash("That machine doesn't exist any more.", 'error')
            return redirect(url_for('config') + '#machines')
        
        # Get form data and validate
        name = request.form.get('name', '').strip()
        ip = request.form.get('ip', '').strip()
        mac = request.form.get('mac', '').strip()
        broadcast_ip = request.form.get('broadcast_ip', '').strip()
        shutdown_delay = request.form.get('shutdown_delay', '').strip()
        auto_wol = 'true' if 'auto_wol' in request.form else 'false'
        ignore_simulation = 'true' if 'ignore_simulation' in request.form else 'false'

        if not all([name, ip, mac]):
            flash('Name, IP address and MAC address are all needed.', 'error')
            return redirect(url_for('config') + '#machines')
        if not validate_ip(ip):
            flash(f'Invalid IP address: {ip}', 'error')
            return redirect(url_for('config') + '#machines')
        if not validate_mac(mac):
            flash(f'Invalid MAC address: {mac}', 'error')
            return redirect(url_for('config') + '#machines')
        if broadcast_ip and not validate_ip(broadcast_ip):
            flash(f'Invalid broadcast IP address: {broadcast_ip}', 'error')
            return redirect(url_for('config') + '#machines')
        
        # Update wake host data
        wake_hosts[section]['NAME'] = name
        wake_hosts[section]['IP'] = ip
        wake_hosts[section]['MAC'] = mac
        wake_hosts[section]['AUTO_WOL'] = auto_wol
        wake_hosts[section]['IGNORE_SIMULATION'] = ignore_simulation
        
        if broadcast_ip:
            wake_hosts[section]['BROADCAST_IP'] = broadcast_ip
        elif 'BROADCAST_IP' in wake_hosts[section]:
            del wake_hosts[section]['BROADCAST_IP']

        if shutdown_delay:
            wake_hosts[section]['SHUTDOWN_DELAY_MINUTES'] = shutdown_delay
        elif 'SHUTDOWN_DELAY_MINUTES' in wake_hosts[section]:
            del wake_hosts[section]['SHUTDOWN_DELAY_MINUTES']

        apply_battery_host_fields(wake_hosts[section])

        write_power_manager_config(pm_config, wake_hosts, schedules)
        flash(f'Saved {name}.', 'success')
        
    except Exception as e:
        flash(f'Error updating host: {str(e)}', 'error')
    
    return redirect(url_for('config') + '#machines')

@app.route('/delete_wake_host/<section>', methods=['POST'])
def delete_wake_host(section):
    """Delete wake host"""
    try:
        pm_config, wake_hosts, schedules = read_power_manager_config()
        
        if section in wake_hosts:
            name = wake_hosts[section].get('NAME', section)
            del wake_hosts[section]
            write_power_manager_config(pm_config, wake_hosts, schedules)
            flash(f'Deleted {name}.', 'success')
        else:
            flash("That machine doesn't exist any more.", 'error')
        
    except Exception as e:
        flash(f'Error deleting host: {str(e)}', 'error')
    
    return redirect(url_for('config') + '#machines')

DAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')


def one_time_is_past(schedule, now=None):
    """True for a one-time schedule whose date and time have already gone by."""
    if schedule.get('TYPE') != 'one-time':
        return False
    try:
        when = datetime.strptime(f"{schedule.get('DATE', '')} {schedule.get('TIME', '')}", '%Y-%m-%d %H:%M')
    except ValueError:
        return True
    return when <= (now or datetime.now())


def schedule_problem(enabling=True):
    """A plain-words reason the submitted schedule can't be saved, or None."""
    form = request.form
    if not form.get('name', '').strip():
        return 'Give the schedule a name.'
    if not re.match(r'^([01]\d|2[0-3]):[0-5]\d$', form.get('time', '')):
        return 'Pick a time.'
    if form.get('action') not in ('start', 'stop'):
        return 'Choose whether it turns test mode on or off.'
    if form.get('type') == 'one-time':
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', form.get('date', '')):
            return 'Pick a date.'
        if enabling and one_time_is_past({'TYPE': 'one-time', 'DATE': form.get('date'),
                                          'TIME': form.get('time')}):
            return 'Pick a date and time in the future.'
    elif form.get('type') == 'recurring':
        if form.get('day_of_week') not in DAYS:
            return 'Pick a day of the week.'
    else:
        return 'Choose one-time or weekly.'
    return None


@app.route('/add_schedule', methods=['POST'])
def add_schedule():
    """Add a new schedule entry."""
    try:
        pm_config, wake_hosts, schedules = read_power_manager_config()
        
        existing_numbers = [int(s.replace('SCHEDULE_', '')) for s in schedules.keys() if s.startswith('SCHEDULE_')]
        next_num = max(existing_numbers) + 1 if existing_numbers else 1
        section_name = f"SCHEDULE_{next_num}"
        
        problem = schedule_problem()
        if problem:
            flash(problem + ' Nothing was saved.', 'error')
            return redirect(url_for('config') + '#schedules')

        new_schedule = {
            'NAME': request.form.get('name'),
            'TYPE': request.form.get('type'),
            'TIME': request.form.get('time'),
            'ACTION': request.form.get('action'),
            'ENABLED': 'true',  # new schedules start active; pause them from the list
        }

        if new_schedule['TYPE'] == 'one-time':
            new_schedule['DATE'] = request.form.get('date')
        else:
            new_schedule['DAY_OF_WEEK'] = request.form.get('day_of_week')
        
        schedules[section_name] = new_schedule
        write_power_manager_config(pm_config, wake_hosts, schedules)
        flash(f'Added the schedule {new_schedule["NAME"]}.', 'success')

    except Exception as e:
        flash(f'Error adding schedule: {str(e)}', 'error')
        
    return redirect(url_for('config') + '#schedules')

@app.route('/edit_schedule/<section>', methods=['POST'])
def edit_schedule(section):
    """Edit an existing schedule entry."""
    try:
        pm_config, wake_hosts, schedules = read_power_manager_config()
        if section not in schedules:
            flash("That schedule doesn't exist any more.", 'error')
            return redirect(url_for('config') + '#schedules')
            
        # Pausing happens in the list; the pop-up only turns a schedule back on
        # when it was opened to pick a new date for a one-time schedule that ran.
        enabling = request.form.get('enable') == '1' or schedules[section].get('ENABLED') == 'true'
        problem = schedule_problem(enabling)
        if problem:
            flash(problem + ' Nothing was saved.', 'error')
            return redirect(url_for('config') + '#schedules')

        updated_schedule = {
            'NAME': request.form.get('name'),
            'TYPE': request.form.get('type'),
            'TIME': request.form.get('time'),
            'ACTION': request.form.get('action'),
            'ENABLED': 'true' if enabling else 'false',
        }

        # Clear old type-specific fields before updating
        schedules[section].pop('DATE', None)
        schedules[section].pop('DAY_OF_WEEK', None)

        if updated_schedule['TYPE'] == 'one-time':
            updated_schedule['DATE'] = request.form.get('date')
        else:
            updated_schedule['DAY_OF_WEEK'] = request.form.get('day_of_week')

        schedules[section].update(updated_schedule)
        write_power_manager_config(pm_config, wake_hosts, schedules)
        flash(f'Saved the schedule {updated_schedule["NAME"]}.', 'success')
        
    except Exception as e:
        flash(f'Error updating schedule: {str(e)}', 'error')

    return redirect(url_for('config') + '#schedules')

@app.route('/delete_schedule/<section>', methods=['POST'])
def delete_schedule(section):
    """Delete a schedule entry."""
    try:
        pm_config, wake_hosts, schedules = read_power_manager_config()
        if section in schedules:
            name = schedules[section].get('NAME', section)
            del schedules[section]
            write_power_manager_config(pm_config, wake_hosts, schedules)
            flash(f'Deleted the schedule {name}.', 'success')
        else:
            flash("That schedule doesn't exist any more.", 'error')
    
    except Exception as e:
        flash(f'Error deleting schedule: {str(e)}', 'error')
        
    return redirect(url_for('config') + '#schedules')

@app.route('/wol/<section>', methods=['POST'])
def wake_host(section):
    """Send Wake-on-LAN to specific host"""
    try:
        pm_config, wake_hosts, _ = read_power_manager_config()
        
        if section not in wake_hosts:
            return jsonify({'success': False, 'message': 'Host not found'})
        
        host = wake_hosts[section]
        mac = host.get('MAC')
        broadcast_ip = host.get('BROADCAST_IP', pm_config.get('DEFAULT_BROADCAST_IP', '192.168.1.255'))
        name = host.get('NAME', section)
        
        if not mac:
            return jsonify({'success': False, 'message': 'MAC address not found'})
        
        if send_wol(mac, broadcast_ip):
            return jsonify({'success': True, 'message': f'Wake-on-LAN sent to {name}'})
        else:
            return jsonify({'success': False, 'message': f'Failed to send Wake-on-LAN to {name}'})
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/status')
def get_status():
    """Get current status of all hosts"""
    try:
        pm_config, wake_hosts, _ = read_power_manager_config()
        recorded_sentinels, recorded_hosts = get_reachability()

        # Check sentinel hosts
        sentinel_hosts = pm_config.get('SENTINEL_HOSTS', '').split()
        sentinel_status = {}
        for host in sentinel_hosts:
            if host:
                clean_host = host.strip().strip('"').strip("'")
                if clean_host:
                    sentinel_status[clean_host] = is_reachable(clean_host, recorded_sentinels)

        # Check wake hosts
        wake_host_status = {}
        for section, params in wake_hosts.items():
            ip = params.get('IP', '')
            if ip:
                wake_host_status[section] = is_reachable(ip, recorded_hosts)
        
        return jsonify({
            'sentinel_status': sentinel_status,
            'wake_host_status': wake_host_status
        })
        
    except Exception as e:
        return jsonify({'error': str(e)})
    
@app.route('/client_statuses')
def get_client_statuses_json():
    """Endpoint to get current client statuses as JSON."""
    return jsonify(get_client_statuses())

@app.route('/battery_status')
def get_battery_status_json():
    """Endpoint to get the latest battery reading for the dashboard panel."""
    return jsonify(get_battery_section())

@app.route('/power_state')
def get_power_state_json():
    """Endpoint to get the full power state, including per-host verdicts."""
    state = read_power_state()
    if state is None:
        return jsonify({
            'available': False,
            'battery': {'enabled': False, 'available': False},
            'hosts': {},
        })
    state['available'] = True
    return jsonify(state)

@app.route('/test_battery', methods=['POST'])
def test_battery_connection():
    """Probe the configured Victron battery monitor and report what we find."""
    try:
        pm_config, _, _ = read_power_manager_config()
        result = BatteryMonitor(pm_config).check_health()
    except Exception as e:
        result = {'enabled': False, 'reachable': False, 'error': str(e)}

    if result.get('error'):
        flash(f"Battery monitor test failed: {result['error']}", 'danger')
    else:
        status = result.get('status') or {}
        mains = {True: 'mains OK', False: 'ON BATTERY', None: 'mains unknown'}[
            status.get('ac_power')
        ]
        flash(
            f"Battery monitor OK - {status.get('voltage')}V, "
            f"SoC {status.get('soc')}%, {mains} "
            f"(reading {status.get('data_age_seconds')}s old).",
            'success',
        )

    return redirect(url_for('config') + '#battery')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80, debug=True)