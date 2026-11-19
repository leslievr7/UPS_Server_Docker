"""First-run sentinel setup and the device picker.

Flow on a new install:
  create password -> "Watch for power cuts?" -> what makes a good sentinel
  (the scan starts here) -> picker -> dashboard

The welcome step is skipped when sentinels are already set up. The guidance
screen is shown once; after that, "Find devices" in Settings and on the
dashboard opens the picker directly.
"""

from flask import flash, jsonify, redirect, render_template, request, session, url_for

import network_scan
import web_auth

DONE = 'onboarding_done'
GUIDE_SEEN = 'sentinel_guide_seen'

NONE_ANSWER = ("None of those devices answer right now. Saving them would make the server think "
               "the power is out and start shutting machines down, so nothing was saved. "
               "Check they're switched on, or choose others.")


def silent_sentinels(gui, config, chosen):
    """Which of a new sentinel list don't answer a ping right now.

    None when the list is the same as before (nothing new to check, and during
    a real power cut the current ones are rightly silent).
    """
    if set(chosen) == set(network_scan.configured_sentinels(config)):
        return None
    live = gui.reachability.check(chosen, ping_cmd=gui.PING_CMD)
    return [ip for ip in chosen if not live.get(ip)]


def quiet_warning(quiet):
    return (f"{', '.join(quiet)} didn't answer just now. That's fine if "
            f"{'it is' if len(quiet) == 1 else 'they are'} only asleep, but a sentinel that never "
            "answers can't tell you about a power cut.")


def register(app, gui):
    """Add the onboarding routes to `app`. `gui` is the web_gui module."""

    def sentinels_set_up():
        config, _, _ = gui.read_power_manager_config()
        return bool(network_scan.configured_sentinels(config))

    def needs_welcome():
        return not web_auth.get_flag(DONE) and not sentinels_set_up()

    def after_setup():
        return url_for('welcome') if needs_welcome() else url_for('index')

    web_auth.after_setup = after_setup

    @app.before_request
    def resume_onboarding():
        # Someone who left mid-way (closed the tab, typed an address) is brought
        # back to the first step until they finish or choose "Later".
        if request.method == 'GET' and request.endpoint in ('index', 'config') \
                and session.get('signed_in') and needs_welcome():
            return redirect(url_for('welcome'))
        return None

    def came_from():
        return 'settings' if request.values.get('from') == 'settings' else 'start'

    def back_url(origin):
        return url_for('config') + '#general' if origin == 'settings' else url_for('index')

    @app.route('/welcome')
    def welcome():
        if not needs_welcome():
            return redirect(url_for('index'))
        return render_template('welcome.html', focus=True)

    @app.route('/welcome/later', methods=['POST'])
    def welcome_later():
        web_auth.set_flag(DONE)
        flash("No problem. You can find devices any time from the dashboard or Settings.", 'info')
        return redirect(url_for('index'))

    @app.route('/sentinels/guide')
    def sentinel_guide():
        origin = came_from()
        first_run = needs_welcome()
        return render_template('sentinel_guide.html', origin=origin, first_run=first_run,
                               focus=first_run)

    @app.route('/sentinels/find')
    def sentinel_picker():
        origin = came_from()
        if not web_auth.get_flag(GUIDE_SEEN) and request.args.get('guide') != 'skip':
            return redirect(url_for('sentinel_guide', **({'from': origin} if origin == 'settings' else {})))
        web_auth.set_flag(GUIDE_SEEN)
        first_run = needs_welcome()
        return render_template('sentinel_picker.html', origin=origin, first_run=first_run,
                               focus=first_run, back=back_url(origin))

    @app.route('/sentinels/scan', methods=['POST'])
    def sentinel_scan_start():
        result = network_scan.start(force=request.args.get('again') == '1')
        return jsonify({'started': result})

    @app.route('/sentinels/scan')
    def sentinel_scan_status():
        state = network_scan.status()
        config, wake_hosts, _ = gui.read_power_manager_config()
        groups = network_scan.describe(state, config, wake_hosts)
        state = state or {}
        return jsonify({
            'status': state.get('status', 'none'),
            'phase': state.get('phase'),
            'checked': state.get('checked', 0),
            'total': state.get('total', 0),
            'error': state.get('error'),
            'found': len(state.get('devices', {})),
            **groups,
        })

    @app.route('/sentinels/save', methods=['POST'])
    def sentinel_save():
        origin = came_from()
        chosen, bad = [], []
        for ip in request.form.getlist('ip'):
            ip = ip.strip()
            if not ip or ip in chosen:
                continue
            (chosen if gui.validate_ip(ip) else bad).append(ip)
        if bad:
            flash(f"{', '.join(bad)} isn't a valid IP address, so nothing was saved.", 'error')
            return redirect(url_for('sentinel_picker', guide='skip',
                                    **({'from': origin} if origin == 'settings' else {})))
        if not chosen:
            flash('Choose at least one device to watch.', 'error')
            return redirect(url_for('sentinel_picker', guide='skip',
                                    **({'from': origin} if origin == 'settings' else {})))

        config, wake_hosts, schedules = gui.read_power_manager_config()
        quiet = silent_sentinels(gui, config, chosen)
        if quiet is not None and len(quiet) == len(chosen):
            flash(NONE_ANSWER, 'error')
            return redirect(url_for('sentinel_picker', guide='skip',
                                    **({'from': origin} if origin == 'settings' else {})))
        found = {ip: d.get('mac') for ip, d in ((network_scan.status() or {}).get('devices') or {}).items()}
        old_macs = network_scan.parse_sentinel_macs(config.get('SENTINEL_MACS'))
        macs = {}
        for ip in chosen:
            mac = found.get(ip) or old_macs.get(ip)
            if mac and not network_scan.is_private_mac(mac):
                macs[ip] = mac
        config['SENTINEL_HOSTS'] = ' '.join(chosen)
        if macs:
            config['SENTINEL_MACS'] = network_scan.format_sentinel_macs(macs)
        else:
            config.pop('SENTINEL_MACS', None)
        gui.write_power_manager_config(config, wake_hosts, schedules)
        web_auth.set_flag(DONE)
        n = len(chosen)
        flash(f"Watching {n} sentinel{'s' if n != 1 else ''}. "
              "If they all stop answering, the server treats it as a power cut.", 'success')
        if quiet:
            flash(quiet_warning(quiet), 'warning')
        return redirect(back_url(origin))

    @app.route('/sentinels/move', methods=['POST'])
    def sentinel_move():
        """Follow a sentinel to its new address (offered on the dashboard)."""
        old, new = request.form.get('ip', '').strip(), request.form.get('new_ip', '').strip()
        config, wake_hosts, schedules = gui.read_power_manager_config()
        current = network_scan.configured_sentinels(config)
        if old not in current or not gui.validate_ip(new):
            flash("That sentinel has already changed. Nothing was saved.", 'error')
            return redirect(url_for('index'))
        macs = network_scan.parse_sentinel_macs(config.get('SENTINEL_MACS'))
        hosts = [new if ip == old else ip for ip in current]
        config['SENTINEL_HOSTS'] = ' '.join(dict.fromkeys(hosts))
        if old in macs:
            macs[new] = macs.pop(old)
            config['SENTINEL_MACS'] = network_scan.format_sentinel_macs(macs)
        gui.write_power_manager_config(config, wake_hosts, schedules)
        flash(f"Now watching {new} instead of {old}.", 'success')
        return redirect(url_for('index'))
