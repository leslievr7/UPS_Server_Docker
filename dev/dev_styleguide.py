"""
Sandbox-only style sample page: /dev/styleguide.

Shows every building block of the new look in one place, with live controls
for the Basecoat style pack, accent colour, corner radius and a box to paste a
tweakcn theme. Registered only by dev/run_local.py, never in production.
"""

import os

from flask import Blueprint, abort, render_template, send_from_directory

DEV_DIR = os.path.dirname(os.path.abspath(__file__))
STYLES_DIR = os.path.join(DEV_DIR, '.styles')  # alternative style packs, not committed
STYLE_PACKS = ['vega', 'nova', 'maia', 'lyra', 'mira', 'luma', 'sera', 'rhea']


def register(app):
    bp = Blueprint('styleguide', __name__, template_folder=os.path.join(DEV_DIR, 'templates'))

    @bp.route('/dev/styleguide')
    def styleguide():
        available = [s for s in STYLE_PACKS
                     if os.path.exists(os.path.join(STYLES_DIR, f'{s}.css'))]
        return render_template('styleguide.html', style_packs=available)

    @bp.route('/dev/basecoat-style/<name>.css')
    def style_pack(name):
        if name not in STYLE_PACKS:
            abort(404)
        return send_from_directory(STYLES_DIR, f'{name}.css', mimetype='text/css')

    app.register_blueprint(bp)
