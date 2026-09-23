"""Portable geometry, ANSI, progress and PTY checks; not a substitute for ISO tests."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / 'configs/airootfs/usr/share/omarchy-iso/install-snake.sh'
PATH = HELPER.with_name('install-snake.path')
DASH = ROOT / 'configs/airootfs/usr/local/bin/omarchy-install-dashboard'
spec = importlib.util.spec_from_file_location('generator', ROOT / 'assets/install-snake/generate.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)
path = generator.generate()
assert PATH.read_text() == ''.join(f'{r} {c}\n' for r, c in path)
print('ok - 380 cells, unique, closed adjacency, exact logo coverage; generated file matches')

with tempfile.TemporaryDirectory() as td:
    temp = Path(td)
    env = dict(os.environ, OMARCHY_SNAKE_PATH=str(PATH), OMARCHY_SNAKE_HELPER=str(HELPER),
               OMARCHY_DASHBOARD_TTY='/dev/null', TERM='xterm-256color', LC_ALL='C.UTF-8')
    header = f'source "{HELPER}"\nsnake_init\n'
    # Includes regressing reports: the rendered length never decreases or
    # exceeds the largest real-work observation, and grows <=6 cells/tick.
    stream = header + '''
last=1; observed=0
for p in 0 0 1 3 3 50 50 50 49 500 500 500 500 999 999 1000; do
  (( p > observed )) && observed=$p
  PROGRESS_PM=$observed
  snake_advance
  (( SNAKE_LENGTH >= last && SNAKE_LENGTH <= 1 + observed * 379 / 1000 && SNAKE_LENGTH <= last + 6 )) || exit 1
  last=$SNAKE_LENGTH
done
PROGRESS_PM=1000
for ((i=0;i<70;i++)); do snake_advance; done
[[ $SNAKE_LENGTH == 380 ]]
'''
    subprocess.run(['bash'], input=stream, text=True, env=env, check=True)
    # Decode monochrome half-block rows to their occupied fine-cell set.
    for n in (1, 2, 190, 380):
        script = header + f'''
CSI=$'\\033['; GREEN=""; WHITE=""; RESET=""; NO_COLOR=1
TOP_ROW=1; NOW=0; SNAKE_LENGTH={n}; term_cols() {{ echo 80; }}
snake_render
'''
        ansi = subprocess.check_output(['bash'], input=script.encode(), env=env).decode()
        import re
        occupied = set()
        for match in re.finditer(r'\x1b\[(\d+);(\d+)H([^\x1b]*)', ansi):
            row = int(match[1])-1
            for col, char in enumerate(match[3]):
                if char in '▀█': occupied.add((row*2, col))
                if char in '▄█': occupied.add((row*2+1, col))
        assert occupied == set(path[:n]), (n, len(occupied))
    print('ok - ANSI snapshots at p=0, 1/379, 0.5, 1 exactly match path prefixes')

    # Exercise actual dashboard progress function with a stalled state and
    # completion timestamp while the child has not yet exited successfully.
    definitions = DASH.read_text().split('# No controllable terminal:')[0]
    state = temp / 'state.json'
    state.write_text(json.dumps({'current_phase': 'Configuring system'}))
    script = f'set -- "{temp / "log"}" "{state}" -- true\n' + definitions + '''
NOW=0; install_progress; first=$PROGRESS_PM
NOW=120; install_progress; [[ $PROGRESS_PM == "$first" ]] || exit 1
printf '{"current_phase":"Installation complete","finished_at":123}' >"$STATE_FILE"
install_progress; [[ $PROGRESS_PM == 999 ]] || exit 1
INSTALL_SUCCEEDED=1; install_progress; [[ $PROGRESS_PM == 1000 ]]
'''
    subprocess.run(['bash'], input=script, text=True, env=env, check=True)
    print('ok - stalls add no progress; 100% requires successful child exit')

# Real Bash dashboard in a PTY; installer child is deliberately simulated.
# Missing font coverage is tested via a synthetic loaded-font map, separately
# from the unresolved real ISO glyph check.
import fcntl
import pty
import select
import signal
import struct
import termios
import time


def run_dashboard(rows, cols, mode, no_color=False, term='xterm-256color'):
    with tempfile.TemporaryDirectory() as td:
        temp = Path(td)
        state = temp / 'state.json'
        state.write_text(json.dumps({'current_phase': 'Preparing live environment'}))
        cmd = 'echo visible-installer-log; sleep 0.4; exit ' + ('7' if mode == 'failure' else '0')
        if mode == 'interrupt': cmd = 'echo visible-installer-log; sleep 30'
        if mode == 'resize': cmd = 'echo visible-installer-log; sleep 2; exit 7'
        ready_r, ready_w = os.pipe()
        pid, fd = pty.fork()
        if pid == 0:
            os.close(ready_w)
            os.read(ready_r, 1)
            os.close(ready_r)
            os.environ.update(OMARCHY_SNAKE_HELPER=str(HELPER), OMARCHY_SNAKE_PATH=str(PATH),
                              OMARCHY_PATH=str(ROOT.parent / 'omarchy'),
                              OMARCHY_UI_INTERACTIVE='no', OMARCHY_UI_AUTO_REBOOT='no',
                              TERM=term, LC_ALL='C.UTF-8')
            if no_color: os.environ['NO_COLOR'] = '1'
            else: os.environ.pop('NO_COLOR', None)
            os.execv(str(DASH), [str(DASH), str(temp/'log'), str(state), '--', 'bash', '-c', cmd])
        os.close(ready_r)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
        os.write(ready_w, b'1')
        os.close(ready_w)
        data = b''
        start = time.monotonic()
        interrupted = False
        resized = 0
        status = None
        eof = False
        while time.monotonic() - start < 45:
            if mode == 'resize':
                elapsed = time.monotonic()-start
                if resized == 0 and elapsed > 0.4:
                    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', 10, 30, 0, 0))
                    resized = 1
                elif resized == 1 and elapsed > 1:
                    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', 25, 80, 0, 0))
                    resized = 2
            if mode == 'interrupt' and not interrupted and time.monotonic()-start > 0.5:
                os.write(fd, b'\x03')
                interrupted = True
            if select.select([fd], [], [], 0.1)[0]:
                try: data += os.read(fd, 65536)
                except OSError:
                    eof = True
                    break
            done, st = os.waitpid(pid, os.WNOHANG)
            if done:
                status = st
                break
        if status is None and eof:
            _, status = os.waitpid(pid, 0)
        if status is None:
            done, status = os.waitpid(pid, os.WNOHANG)
            if not done:
                os.kill(pid, signal.SIGTERM)
                os.waitpid(pid, 0)
                Path('/tmp/snake-timeout.ansi').write_bytes(data)
                raise AssertionError(f'dashboard timed out: {rows}x{cols} {mode}; last bytes: {data[-300:]!r}')
        os.close(fd)
        return os.waitstatus_to_exitcode(status), data

for rows, cols, mode in [(25,80,'success'), (50,120,'failure'), (25,80,'failure'),
                         (25,80,'interrupt'), (10,30,'success')]:
    status, data = run_dashboard(rows, cols, mode)
    assert status == {'success':0, 'failure':7, 'interrupt':130}[mode], (mode,status)
    if rows >= 22:
        assert b'\x1b[?25h' in data, 'cursor not restored'
    else:
        assert b'\x1b' not in data, 'small-terminal plain fallback emitted escapes'
    if mode == 'failure':
        assert b'Omarchy installation stopped' in data and b'visible-installer-log' in data
    if mode == 'success' and rows >= 22:
        assert b'Installed Omarchy' in data
    print(f'ok - {cols}x{rows} {mode}, exit {status}, cleanup and output')
status, data = run_dashboard(25, 80, 'failure', no_color=True)
assert not re.search(rb'\x1b\[[0-9;]*m', data), 'NO_COLOR emitted SGR'
print('ok - NO_COLOR has no SGR colour escapes')

with tempfile.TemporaryDirectory() as td:
    temp = Path(td)
    env = dict(os.environ, OMARCHY_SNAKE_HELPER=str(HELPER), OMARCHY_SNAKE_PATH=str(PATH),
               OMARCHY_DASHBOARD_TTY='/dev/null', OMARCHY_UI_INTERACTIVE='no',
               OMARCHY_UI_AUTO_REBOOT='no', TERM='dumb')
    result = subprocess.run([str(DASH), str(temp/'log'), str(temp/'state'), '--',
                             'bash', '-c', 'echo non-tty-log; exit 7'], env=env,
                            capture_output=True, timeout=10)
    assert result.returncode == 7 and b'non-tty-log' in result.stdout and b'\x1b' not in result.stdout, (result.returncode, repr(result.stdout), repr(result.stderr))
    print('ok - non-TTY streams log, preserves failure status, emits no escapes')

    # Font-map fixtures exercise dispatch only; they do not verify ISO fonts.
    for mapping, expected in [('0x1 U+2580\n0x2 U+2584\n0x3 U+2588', 1),
                              ('0x1 U+2580\n0x3 U+2588', 0)]:
        fixture = temp / 'getunimap'
        fixture.write_text('#!/bin/sh\ncat <<\'MAP\'\n' + mapping + '\nMAP\n')
        fixture.chmod(0o755)
        env.update(PATH=str(temp)+':'+os.environ['PATH'], TERM='linux', LC_ALL='C.UTF-8')
        script = f'source "{HELPER}"\nsnake_init\n[[ $SNAKE_ENABLED == {expected} ]]\n'
        subprocess.run(['bash'], input=script, text=True, env=env, check=True)
    print('ok - loaded font map gates half-block rendering; missing glyph falls back')

status, data = run_dashboard(50, 120, 'resize')
assert status == 7 and b'visible-installer-log' in data and b'\x1b[?25h' in data, (status, repr(data[-700:]))
assert data.count(b'\x1b[2J') == 4, ('expected four layout/failure clears', data.count(b'\x1b[2J'), data[-100:])
print('ok - live resize 120x50 -> 30x10 -> 80x25; clears only on layout changes/failure')

# Replay changed rows through the complete growth sequence, not just isolated
# frames. Every intermediate screen must equal its exact path prefix.
env = dict(os.environ, OMARCHY_SNAKE_PATH=str(PATH), TERM='xterm-256color', LC_ALL='C.UTF-8')
script = f'source "{HELPER}"\nsnake_init\n' + r"""
CSI=$'\033['; GREEN=""; WHITE=""; RESET=""; NO_COLOR=1
TOP_ROW=1; NOW=0; term_cols() { echo 80; }
for ((n=1;n<=380;n++)); do
  SNAKE_LENGTH=$n
  snake_render
  printf '\036'
done
"""
frames = subprocess.check_output(['bash'], input=script.encode(), env=env).decode().split('\x1e')
screen = [' '*30 for _ in range(15)]
for n, ansi in enumerate(frames[:-1], 1):
    for match in re.finditer(r'\x1b\[(\d+);(\d+)H([^\x1b]*)', ansi):
        screen[int(match[1])-1] = match[3]
    occupied = set()
    for r, row in enumerate(screen):
        for c, char in enumerate(row):
            if char in '▀█': occupied.add((2*r,c))
            if char in '▄█': occupied.add((2*r+1,c))
    assert occupied == set(path[:n]), n
print('ok - all 380 incremental screens exactly match their path prefix')
