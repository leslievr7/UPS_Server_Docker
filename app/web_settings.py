"""Settings saves, one per tab.

The old settings page saved everything in one big form, so saving email
settings also re-saved test mode, sentinels and battery settings from whatever
the page happened to hold. Here each tab has its own route that:

1. reads power_manager.conf,
2. changes only the keys that belong to that tab,
3. writes it back with the existing writer, which keeps every [WAKE_HOST_X]
   and [SCHEDULE_X] block.

Test mode has its own route, so no other save can switch it on or off.
The email password is never sent to the browser; leaving the field blank keeps
the saved one.
"""

from flask import flash, jsonify, redirect, request, url_for

import network_scan
import web_onboarding

GENERAL_INTS = (
    # form field, config key, lowest, highest, plain name
    ('wol_delay_minutes', 'WOL_DELAY_MINUTES', 1, 60, 'Wake-up delay'),
    ('wol_confirm_timeout_minutes', 'WOL_CONFIRM_TIMEOUT_MINUTES', 0, 120, 'Wake-up check'),
    ('client_stale_timeout_minutes', 'CLIENT_STALE_TIMEOUT_MINUTES', 1, 60, 'Stale after'),
)

NOTIFY_KEYS = ('NOTIFY_POWER_FAIL', 'NOTIFY_POWER_RESTORED', 'NOTIFY_WOL_FAILURE',
               'NOTIFY_CLIENT_SHUTDOWN', 'NOTIFY_CLIENT_STALE', 'NOTIFY_APP_ERROR',
               'NOTIFY_SIMULATION_MODE')

# Switches that save the moment they're flipped, and how the toast names them.
SWITCH_LABELS = {
    'NOTIFY_POWER_FAIL': 'Power cut emails',
    'NOTIFY_POWER_RESTORED': 'Power back emails',
    'NOTIFY_CLIENT_SHUTDOWN': 'Machine turning off emails',
    'NOTIFY_WOL_FAILURE': 'Failed wake-up emails',
    'NOTIFY_CLIENT_STALE': 'Script stopped checking in emails',
    'NOTIFY_SIMULATION_MODE': 'Test mode emails',
    'NOTIFY_APP_ERROR': 'Server error emails',
    'DEBUG_MODE': 'Detailed logging',
}

BATTERY_TEXT = (
    ('battery_api_timeout', 'BATTERY_API_TIMEOUT'),
    ('battery_max_data_age', 'BATTERY_MAX_DATA_AGE'),
    ('battery_capacity_ah', 'BATTERY_CAPACITY_AH'),
    ('battery_default_critical_voltage', 'BATTERY_DEFAULT_CRITICAL_VOLTAGE'),
    ('battery_default_shutdown_voltage', 'BATTERY_DEFAULT_SHUTDOWN_VOLTAGE'),
    ('battery_default_shutdown_soc', 'BATTERY_DEFAULT_SHUTDOWN_SOC'),
    ('battery_default_min_runtime_minutes', 'BATTERY_DEFAULT_MIN_RUNTIME_MINUTES'),
    ('battery_default_wol_min_soc', 'BATTERY_DEFAULT_WOL_MIN_SOC'),
    ('battery_sim_ac_power', 'BATTERY_SIM_AC_POWER'),
    ('battery_sim_soc', 'BATTERY_SIM_SOC'),
    ('battery_sim_voltage', 'BATTERY_SIM_VOLTAGE'),
    ('battery_sim_current', 'BATTERY_SIM_CURRENT'),
    ('battery_sim_remaining', 'BATTERY_SIM_REMAINING'),
)


def _number(text):
    try:
        float(text)
        return True
    except ValueError:
        return False


def _switch(name):
    return 'true' if request.form.get(name) in ('true', 'on', '1') else 'false'


def register(app, gui):
    """Add the settings routes to `app`. `gui` is the web_gui module."""

    def back(tab, message=None, category='success'):
        if message:
            flash(message, category)
        return redirect(url_for('config') + '#' + tab)

    def apply(updates):
        config, wake_hosts, schedules = gui.read_power_manager_config()
        config.update(updates)
        for key in [k for k, v in updates.items() if v is None]:
            config.pop(key, None)
        gui.write_power_manager_config(config, wake_hosts, schedules)

    def save(updates, tab, message):
        apply(updates)
        return back(tab, message)

    @app.route('/settings/general', methods=['POST'])
    def save_general():
        updates = {}
        sentinels = request.form.get('sentinel_hosts', '').replace(',', ' ').split()
        bad = [ip for ip in sentinels if not gui.validate_ip(ip)]
        if bad:
            return back('general', f"{', '.join(bad)} isn't a valid IP address. Nothing was saved.", 'error')
        current = gui.read_power_manager_config()[0]
        quiet = web_onboarding.silent_sentinels(gui, current, sentinels) if sentinels else None
        if quiet is not None and len(quiet) == len(sentinels):
            return back('general', web_onboarding.NONE_ANSWER, 'error')
        updates['SENTINEL_HOSTS'] = ' '.join(sentinels)
        # Forget the remembered device of any sentinel that was removed.
        macs = network_scan.parse_sentinel_macs(current.get('SENTINEL_MACS'))
        if macs:
            kept = {ip: mac for ip, mac in macs.items() if ip in sentinels}
            updates['SENTINEL_MACS'] = network_scan.format_sentinel_macs(kept) or None
        names = network_scan.parse_sentinel_names(current.get('SENTINEL_NAMES'))
        if names:
            kept = {ip: n for ip, n in names.items() if ip in sentinels}
            updates['SENTINEL_NAMES'] = network_scan.format_sentinel_names(kept) or None

        for field, key, low, high, name in GENERAL_INTS:
            raw = request.form.get(field, '').strip()
            if not raw.isdigit() or not low <= int(raw) <= high:
                return back('general', f'{name} must be a whole number from {low} to {high}. Nothing was saved.', 'error')
            updates[key] = str(int(raw))

        broadcast = request.form.get('default_broadcast_ip', '').strip()
        if not gui.validate_ip(broadcast):
            return back('general', "The broadcast address isn't a valid IP address. Nothing was saved.", 'error')
        updates['DEFAULT_BROADCAST_IP'] = broadcast
        if quiet:
            flash(web_onboarding.quiet_warning(quiet), 'warning')
        return save(updates, 'general', 'General settings saved.')

    @app.route('/settings/test_mode', methods=['POST'])
    def save_test_mode():
        on = request.form.get('state') == 'on'
        message = ('Test mode is on. Machines are being told the power is out.' if on
                   else 'Test mode is off. Machines are being told the power is back.')
        if request.form.get('next') == 'dashboard':
            # "Stop test mode" on the dashboard banner: stay on the dashboard.
            apply({'POWER_SIMULATION_MODE': 'true' if on else 'false'})
            flash(message, 'success')
            return redirect(url_for('index'))
        return save({'POWER_SIMULATION_MODE': 'true' if on else 'false'}, 'general', message)

    @app.route('/settings/switch', methods=['POST'])
    def save_switch():
        """One on/off setting, saved the moment its switch is flipped."""
        key = request.form.get('name', '')
        if key not in SWITCH_LABELS:
            return jsonify({'success': False, 'message': "That setting can't be changed here."}), 400
        on = request.form.get('state') == 'on'
        apply({key: 'true' if on else 'false'})
        return jsonify({'success': True, 'on': on,
                        'message': f"{SWITCH_LABELS[key]} {'on' if on else 'off'}."})

    @app.route('/schedule/<section>/enabled', methods=['POST'])
    def save_schedule_enabled(section):
        """Pause or resume a schedule from the list."""
        on = request.form.get('state') == 'on'
        config, wake_hosts, schedules = gui.read_power_manager_config()
        schedule = schedules.get(section)
        if schedule is None:
            return jsonify({'success': False, 'message': "That schedule doesn't exist any more. Reload the page."}), 404
        name = schedule.get('NAME', section)
        if on and gui.one_time_is_past(schedule):
            # Its time has gone by: the page opens the schedule to pick a new one.
            return jsonify({'success': False, 'needs_date': True,
                            'message': f'Pick a new date and time for {name}.'})
        schedule['ENABLED'] = 'true' if on else 'false'
        gui.write_power_manager_config(config, wake_hosts, schedules)
        return jsonify({'success': True, 'on': on,
                        'message': f"{name} {'resumed' if on else 'paused'}."})

    @app.route('/settings/notifications', methods=['POST'])
    def save_notifications():
        updates = {}
        port = request.form.get('smtp_port', '').strip()
        if port and not (port.isdigit() and 1 <= int(port) <= 65535):
            return back('notifications', 'The port must be a number from 1 to 65535. Nothing was saved.', 'error')
        sender = request.form.get('smtp_sender_email', '').strip()
        recipients = request.form.get('smtp_recipients', '').strip()
        if sender and not gui.validate_email_list(sender):
            return back('notifications', "The sender address doesn't look like an email address. Nothing was saved.", 'error')
        if recipients and not gui.validate_email_list(recipients):
            return back('notifications', "One of the recipients doesn't look like an email address. Nothing was saved.", 'error')

        tls = request.form.get('smtp_use_tls', 'auto')
        updates.update({
            'SMTP_SERVER': request.form.get('smtp_server', '').strip(),
            'SMTP_PORT': port,
            'SMTP_USE_TLS': tls if tls in ('auto', 'true', 'false') else 'auto',
            'SMTP_USER': request.form.get('smtp_user', '').strip(),
            'SMTP_SENDER_NAME': request.form.get('smtp_sender_name', '').strip(),
            'SMTP_SENDER_EMAIL': sender,
            'SMTP_RECIPIENTS': ','.join(r.strip() for r in recipients.split(',') if r.strip()),
        })
        # The saved password never reaches the browser, so a blank field means
        # "keep it"; removing it is a separate, explicit choice.
        password = request.form.get('smtp_password', '')
        if request.form.get('clear_smtp_password'):
            updates['SMTP_PASSWORD'] = None
        elif password:
            updates['SMTP_PASSWORD'] = password

        # The "Email me when" switches save on their own (save_switch).
        return save(updates, 'notifications', 'Email settings saved.')

    @app.route('/settings/battery', methods=['POST'])
    def save_battery():
        updates = {'BATTERY_ENABLED': _switch('battery_enabled'),
                   'BATTERY_SIMULATION': _switch('battery_simulation')}
        url = request.form.get('battery_api_url', '').strip()
        if updates['BATTERY_ENABLED'] == 'true' and not url.startswith(('http://', 'https://')):
            return back('battery', 'The battery monitor address must start with http:// or https://. Nothing was saved.', 'error')
        updates['BATTERY_API_URL'] = url

        mode = request.form.get('battery_decision_mode', 'observe')
        updates['BATTERY_DECISION_MODE'] = mode if mode in ('observe', 'enforce') else 'observe'
        source = request.form.get('battery_default_power_source', 'sentinel')
        updates['BATTERY_DEFAULT_POWER_SOURCE'] = source if source in ('sentinel', 'battery', 'both') else 'sentinel'

        for field, key in BATTERY_TEXT:
            value = request.form.get(field, '').strip()
            if field == 'battery_sim_ac_power':
                updates[key] = value if value in ('true', 'false') else ''
                continue
            if value and not _number(value):
                return back('battery', f'"{value}" isn\'t a number. Nothing was saved.', 'error')
            updates[key] = value
        return save(updates, 'battery', 'Battery monitor settings saved.')
