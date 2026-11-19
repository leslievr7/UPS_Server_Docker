"""One careful way to read and change power_manager.conf.

Both writers (the web pages and power_manager.py) save through this module, so:

- They take turns: a lock on a small side file (.power_manager.conf.lock) next
  to the config. The lock is on a side file, not the config itself, because the
  config is replaced on every save and a lock on the old copy would be lost.
- A save writes a complete new copy and swaps it in with one rename, so the
  readers (api.py, send_email.py, the power manager's own reader), which don't
  lock, only ever see the old file or the new one, never half of each.
- Only the settings that actually changed are touched. Comments, blank lines,
  ordering and settings this version doesn't know about are kept as they are.
- The previous version is kept as power_manager.conf.bak.

A save is described as "what I read" (base) and "what I want" (new). Only the
difference between the two is applied, onto the file as it is now. If the
power manager switched test mode off after the page was loaded, saving the
Notifications tab doesn't switch it back on.

The parser matches read_power_manager_config() in power_manager.py and api.py:
'#' lines are comments; keys under [WAKE_HOST_*] and [SCHEDULE_*] belong to that
section; keys anywhere else (including under any other [section]) are main
settings; quotes around values are removed; the last duplicate wins.
"""

import contextlib
import copy
import errno
import fcntl
import os
import stat
import tempfile

SECTION_PREFIXES = ('WAKE_HOST_', 'SCHEDULE_')


# --- reading -------------------------------------------------------------------

def _clean_value(raw):
    return raw.strip().strip('"\'').strip()


def _known_section(stripped):
    """'[WAKE_HOST_1]' -> 'WAKE_HOST_1'; any other header -> None (main settings)."""
    name = stripped[1:-1]
    return name if name.startswith(SECTION_PREFIXES) else None


def parse_text(text):
    """Return (config, wake_hosts, schedules) from the file's text."""
    config, wake_hosts, schedules = {}, {}, {}
    current = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            current = _known_section(line)
            if current:
                (wake_hosts if current.startswith('WAKE_HOST_') else schedules)[current] = {}
            continue
        if '=' in line:
            key, value = line.split('=', 1)
            key, value = key.strip(), _clean_value(value)
            if current:
                (wake_hosts if current.startswith('WAKE_HOST_') else schedules)[current][key] = value
            else:
                config[key] = value
    return config, wake_hosts, schedules


def read(path):
    """Return (config, wake_hosts, schedules). A missing file reads as empty."""
    try:
        with open(path, 'r') as f:
            return parse_text(f.read())
    except FileNotFoundError:
        return {}, {}, {}


# --- taking turns --------------------------------------------------------------

def lock_path(path):
    folder, name = os.path.split(path)
    return os.path.join(folder, f'.{name}.lock')


@contextlib.contextmanager
def locked(path):
    """Hold the writers' lock for `path` (waits for any other writer to finish)."""
    fd = os.open(lock_path(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- describing changes ----------------------------------------------------------

def diff(base, new):
    """List the changes that turn `base` into `new` (both (config, hosts, schedules))."""
    changes = []
    base_config, new_config = base[0], new[0]
    for key, value in new_config.items():
        if base_config.get(key) != value:
            changes.append(('set', None, key, value))
    for key in base_config:
        if key not in new_config:
            changes.append(('del', None, key))

    for base_sections, new_sections in ((base[1], new[1]), (base[2], new[2])):
        for name, values in new_sections.items():
            if name not in base_sections:
                changes.append(('add_section', name, dict(values)))
                continue
            old = base_sections[name]
            for key, value in values.items():
                if old.get(key) != value:
                    changes.append(('set', name, key, value))
            for key in old:
                if key not in values:
                    changes.append(('del', name, key))
        for name in base_sections:
            if name not in new_sections:
                changes.append(('del_section', name))
    return changes


# --- applying changes to the file's lines ----------------------------------------

def _index(lines):
    """For each line: (scope, kind, key). scope is a known section name or None (main)."""
    out, scope = [], None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            out.append((scope, 'other', None))
        elif stripped.startswith('[') and stripped.endswith(']'):
            scope = _known_section(stripped)
            out.append((scope, 'header', None))
        elif '=' in stripped:
            out.append((scope, 'kv', stripped.split('=', 1)[0].strip()))
        else:
            out.append((scope, 'other', None))
    return out


def _kv_line(old_line, key, value):
    indent = old_line[:len(old_line) - len(old_line.lstrip())] if old_line else ''
    return f'{indent}{key}="{value}"\n'


def _section_header_at(idx, name):
    for i, (scope, kind, _) in enumerate(idx):
        if kind == 'header' and scope == name:
            return i
    return None


def _body_end(idx, name):
    """Index just after the section's header or last setting (its trailing comments stay after)."""
    end = _section_header_at(idx, name)
    if end is None:
        return len(idx)
    for i, (scope, kind, _) in enumerate(idx):
        if scope == name and kind == 'kv':
            end = i
    return end + 1


def _set(lines, scope, key, value):
    idx = _index(lines)
    if scope is not None and _section_header_at(idx, scope) is None:
        return False  # the section was deleted meanwhile; don't bring it back
    hits = [i for i, (s, kind, k) in enumerate(idx) if s == scope and kind == 'kv' and k == key]
    for i in hits:
        if _clean_value(lines[i].split('=', 1)[1]) != value:
            lines[i] = _kv_line(lines[i], key, value)
    if hits:
        return True
    if scope is not None:
        lines.insert(_body_end(idx, scope), f'{key}="{value}"\n')
        return True
    main = [i for i, (s, kind, _) in enumerate(idx) if s is None and kind == 'kv']
    if main:
        pos = main[-1] + 1
    else:
        # No main settings yet: before the first machine/schedule section.
        pos = next((i for i, (s, kind, _) in enumerate(idx) if kind == 'header' and s), len(lines))
        while pos > 0 and not lines[pos - 1].strip():
            pos -= 1
    lines.insert(pos, f'{key}="{value}"\n')
    return True


def _del(lines, scope, key):
    idx = _index(lines)
    for i in reversed(range(len(lines))):
        s, kind, k = idx[i]
        if s == scope and kind == 'kv' and k == key:
            del lines[i]


def _add_section(lines, name, values):
    idx = _index(lines)
    prefix = next(p for p in SECTION_PREFIXES if name.startswith(p))
    same_kind = [s for s, kind, _ in idx if kind == 'header' and s and s.startswith(prefix)]
    if name in same_kind:
        # Someone else added a section with this number meanwhile: take the next free one.
        numbers = [int(s[len(prefix):]) for s in same_kind if s[len(prefix):].isdigit()]
        name = f'{prefix}{max(numbers) + 1}'
    pos = _body_end(idx, same_kind[-1]) if same_kind else len(lines)
    if lines and not lines[-1].endswith('\n'):
        lines[-1] += '\n'
    block = ['\n', f'[{name}]\n'] + [f'{k}="{v}"\n' for k, v in values.items()]
    lines[pos:pos] = block


def _del_section(lines, name):
    idx = _index(lines)
    start = _section_header_at(idx, name)
    if start is None:
        return
    end = _body_end(idx, name)
    # Remove the header, its settings and blank lines; keep comments (they often
    # head the next group). Also drop the blank line that separated it.
    keep = [i for i in range(start, end) if idx[i][1] == 'other' and lines[i].strip()]
    removed = [lines[i] for i in keep]
    if start > 0 and not lines[start - 1].strip():
        start -= 1
    lines[start:end] = removed


def apply(text, changes):
    """Return the file text with `changes` applied, touching nothing else."""
    lines = text.splitlines(keepends=True)
    for change in changes:
        op = change[0]
        if op == 'set':
            _set(lines, change[1], change[2], change[3])
        elif op == 'del':
            _del(lines, change[1], change[2])
        elif op == 'add_section':
            _add_section(lines, change[1], change[2])
        elif op == 'del_section':
            _del_section(lines, change[1])
    return ''.join(lines)


# --- writing ---------------------------------------------------------------------

def _fsync_folder(folder):
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_file(path, text):
    folder = os.path.dirname(path) or '.'
    try:
        st = os.stat(path)
    except FileNotFoundError:
        st = None

    if st is not None:
        try:
            with open(path, 'r') as src, open(path + '.bak', 'w') as bak:
                bak.write(src.read())
        except OSError:
            pass  # a backup is a bonus; never block the save on it

    fd, tmp = tempfile.mkstemp(prefix=f'.{os.path.basename(path)}.', suffix='.tmp', dir=folder)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if st is not None:
            os.chmod(tmp, stat.S_IMODE(st.st_mode))
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except OSError:
                pass
        try:
            os.replace(tmp, path)
        except OSError as e:
            # The file itself is bind-mounted (not its folder), so it can't be
            # swapped. Fall back to rewriting it in place, still under the lock.
            if e.errno not in (errno.EBUSY, errno.EXDEV, errno.EPERM, errno.EACCES):
                raise
            with open(path, 'w') as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.unlink(tmp)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    _fsync_folder(folder)


def _current_text(path):
    try:
        with open(path, 'r') as f:
            return f.read()
    except FileNotFoundError:
        return ''


def save(path, new, base=None):
    """Make the file match `new`, changing only what differs from `base`.

    `base` is the (config, wake_hosts, schedules) the caller read before making
    its changes. Without it, the file as it is now is the base, so the file ends
    up exactly matching `new`. Returns True if the file changed.
    """
    with locked(path):
        text = _current_text(path)
        if base is None:
            base = parse_text(text)
        updated = apply(text, diff(base, new))
        if updated == text:
            return False
        _write_file(path, updated)
        return True


def set_value(path, key, value, section=None):
    """Change one setting (in `section`, or a main setting). Returns False if the
    section no longer exists."""
    with locked(path):
        text = _current_text(path)
        lines = text.splitlines(keepends=True)
        if not _set(lines, section, key, str(value)):
            return False
        updated = ''.join(lines)
        if updated != text:
            _write_file(path, updated)
        return True


def snapshot(parsed):
    """A copy of (config, wake_hosts, schedules) that later edits won't change."""
    return copy.deepcopy(parsed)
