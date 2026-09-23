#!/usr/bin/python

"""The install dashboard's snake: its path, what it draws, and that it never
draws more progress than the installer has made.

The path tests load the generator itself. The render and progress tests
source the dashboard and call its functions with a controlled clock, so a
thirty-minute stall is one line rather than thirty minutes. The end-to-end
tests run the real dashboard in a pty against a stand-in installer and read
the screen back through a small VT emulator.
"""

import importlib.util
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "builder/generate-snake-path.py"
PATH_FILE = ROOT / "configs/airootfs/usr/share/omarchy-iso/snake-path"
DASHBOARD = ROOT / "configs/airootfs/usr/local/bin/omarchy-install-dashboard"

# The mark at 100%, as the console draws it: the brief's 15x15 grid at 2x,
# two path rows per character row.
FULL_MARK = """\
██████████████████████████████
██            ██            ██
██  ████████████      ████  ██
██  ██                  ██  ██
██  ██                  ██  ██
██  ██                  ██  ██
██  ██                  ██  ██
██████                  ██  ██
██  ██                  ██  ██
██  ██                  ██  ██
██  ██                  ██  ██
██  ██                  ██  ██
██  ██████████████████████  ██
██            ██            ██
████████████████  ████████████"""

# Band floors and ceilings from install_progress, restated so the bound below
# is computed independently of the code under test.
BANDS = {
    "Starting installation": (10, 25),
    "Preparing live environment": (25, 50),
    "Preparing install target": (50, 65),
    "Installing Arch + Omarchy": (65, 700),
    "Configuring hibernation": (700, 720),
    "Configuring system": (720, 850),
    "Staging provisioning": (850, 855),
    "Finalizing Limine boot": (855, 950),
    "Finalizing user": (950, 978),
    "Configuring login": (978, 988),
    "Configuring DNS resolver": (988, 994),
    "Validating boot setup": (994, 998),
    "Creating factory snapshot": (998, 1000),
}
PKG_PHASE = "Installing Arch + Omarchy"


def load_generator():
    spec = importlib.util.spec_from_file_location("generate_snake_path", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def committed_path():
    return [
        tuple(map(int, line.split()))
        for line in PATH_FILE.read_text().splitlines()
        if line and not line.startswith("#")
    ]


# ── VT ──────────────────────────────────────────────────────────────────────

TOKEN = re.compile(r"\x1b\[([?0-9;]*)([A-Za-z])|\x1b([78])|([\r\n])|([^\x1b\r\n])")


class Screen:
    """CUP, ED, EL, SGR colours, DECTCEM, CR/LF: what the dashboard emits."""

    def __init__(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.cells = [[(" ", None, None)] * cols for _ in range(rows)]
        self.y = self.x = 0
        self.fg = self.bg = None
        self.cursor_visible = True
        self.saved = (0, 0)

    def feed(self, data, on_token=None):
        for m in TOKEN.finditer(data):
            params, final, esc, ctl, ch = m.groups()
            if final:
                self._csi(params, final)
            elif esc:
                if esc == "7":
                    self.saved = (self.y, self.x)
                else:
                    self.y, self.x = self.saved
            elif ctl == "\r":
                self.x = 0
            elif ctl == "\n":
                self.y = min(self.y + 1, self.rows - 1)
            else:
                if self.x < self.cols:
                    row = list(self.cells[self.y])
                    row[self.x] = (ch, self.fg, self.bg)
                    self.cells[self.y] = row
                self.x += 1
                if self.x >= self.cols:
                    self.x = 0
                    self.y = min(self.y + 1, self.rows - 1)
            if on_token:
                on_token(self)

    def _csi(self, params, final):
        if params.startswith("?"):
            if params == "?25":
                self.cursor_visible = final == "h"
            return
        nums = [int(p) if p else 0 for p in params.split(";")] if params else []
        blank = [(" ", None, None)] * self.cols
        if final == "H":
            r = (nums[0] if nums else 1) or 1
            c = (nums[1] if len(nums) > 1 else 1) or 1
            self.y, self.x = min(r, self.rows) - 1, min(c, self.cols) - 1
        elif final == "J":
            if (nums or [0])[0] == 2:
                self.cells = [blank for _ in range(self.rows)]
            else:
                self.cells[self.y] = self.cells[self.y][: self.x] + blank[self.x:]
                for y in range(self.y + 1, self.rows):
                    self.cells[y] = blank
        elif final == "K":
            self.cells[self.y] = blank
        elif final == "m":
            for n in nums or [0]:
                if n == 0:
                    self.fg = self.bg = None
                elif 30 <= n <= 37 or 90 <= n <= 97:
                    self.fg = n
                elif 40 <= n <= 47:
                    self.bg = n

    def text(self, top=0, left=0, rows=None, cols=None):
        rows = self.rows - top if rows is None else rows
        cols = self.cols - left if cols is None else cols
        return "\n".join(
            "".join(c[0] for c in row[left:left + cols]).rstrip()
            for row in self.cells[top:top + rows]
        )

    def shape(self, top=0, left=0, rows=None, cols=None):
        """text(), with a two-colour ▀ (head over body) read as the full cell it fills."""
        rows = self.rows - top if rows is None else rows
        cols = self.cols - left if cols is None else cols
        return "\n".join(
            "".join("█" if ch == "▀" and bg is not None else ch
                    for ch, _fg, bg in row[left:left + cols]).rstrip()
            for row in self.cells[top:top + rows]
        )


def halves(screen, top, left, rows, cols):
    """Path cells lit in a character box: (row, col) -> colour."""
    lit = {}
    for R in range(rows):
        for c in range(cols):
            ch, fg, bg = screen.cells[top + R][left + c]
            if ch in "▀█":
                lit[(2 * R, c)] = fg
            if ch == "▄":
                lit[(2 * R + 1, c)] = fg
            elif ch == "█":
                lit[(2 * R + 1, c)] = fg
            elif ch == "▀" and bg is not None:
                lit[(2 * R + 1, c)] = bg - 10
    return lit


def expected_mark(n, path):
    """Half-block text for path[:n], the way the console should show it."""
    on = set(path[:n])
    lines = []
    for R in range(15):
        line = ""
        for c in range(30):
            t, b = (2 * R, c) in on, (2 * R + 1, c) in on
            line += "█" if t and b else "▀" if t else "▄" if b else " "
        lines.append(line.rstrip())
    return "\n".join(lines)


def bash(script, **env):
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, check=True, timeout=120,
        env={**os.environ, **env},
    ).stdout


PRELUDE = f"""
set -euo pipefail
export OMARCHY_DASHBOARD_TTY=/dev/null OMARCHY_SNAKE_PATH_FILE='{PATH_FILE}'
source '{DASHBOARD}' /dev/null "$STATE" -- true
snake_load
"""


# ── Path ────────────────────────────────────────────────────────────────────

class SnakePathTest(unittest.TestCase):
    def test_generator_assertions_hold(self):
        path = load_generator().build_path()
        self.assertEqual(len(path), 380)
        self.assertEqual(len(set(path)), 380)
        for a, b in zip(path, path[1:] + path[:1]):
            self.assertEqual(abs(a[0] - b[0]) + abs(a[1] - b[1]), 1, f"{a} -> {b}")

    def test_path_cells_are_the_mark_at_twice_the_resolution(self):
        gen = load_generator()
        fine = {
            (2 * r + i, 2 * c + j)
            for r, c in gen.mark_cells() for i in (0, 1) for j in (0, 1)
        }
        self.assertEqual(set(committed_path()), fine)

    def test_committed_output_is_current(self):
        output = subprocess.run(
            ["python3", str(GENERATOR)], capture_output=True, text=True, check=True
        ).stdout
        self.assertEqual(output, PATH_FILE.read_text())

    def test_starts_beside_the_mouth_heading_up(self):
        path = committed_path()
        self.assertEqual(path[0], (27, 14))
        self.assertEqual(path[1], (26, 14))


# ── Render ──────────────────────────────────────────────────────────────────

@unittest.skipUnless(shutil.which("jq"), "the dashboard reads state with jq")
class SnakeRenderTest(unittest.TestCase):
    def render(self, work_pm, done=0, no_color=False):
        with tempfile.TemporaryDirectory() as tmp:
            out = bash(PRELUDE + f"""
SNAKE_TOP=1 SNAKE_LEFT=1
WORK_PM={work_pm} SNAKE_DONE={done}
snake_update_target
while (( SNAKE_SHOWN < SNAKE_TARGET )); do snake_frame; done
""", STATE=f"{tmp}/state.json", **({"NO_COLOR": "1"} if no_color else {}))
        screen = Screen(15, 30)
        screen.feed(out)
        return screen, out

    def test_snapshots(self):
        path = committed_path()
        # p = 0, 1/379, 0.5 in per-mille, and the finished install.
        for work_pm, done, n in ((0, 0, 1), (3, 0, 2), (500, 0, 190), (1000, 1, 380)):
            with self.subTest(n=n):
                screen, _ = self.render(work_pm, done)
                self.assertEqual(screen.shape(), expected_mark(n, path))
                lit = halves(screen, 0, 0, 15, 30)
                self.assertEqual(set(lit), set(path[:n]))
                self.assertEqual(lit[path[n - 1]], 36, "head is cyan")
                self.assertEqual({lit[p] for p in path[: n - 1]} - {32}, set(), "body is green")

    def test_finished_mark_is_the_logo(self):
        screen, _ = self.render(1000, done=1)
        self.assertEqual(screen.shape(), FULL_MARK)

    def test_last_cell_waits_for_the_installer_to_exit(self):
        screen, _ = self.render(1000, done=0)
        self.assertEqual(len(halves(screen, 0, 0, 15, 30)), 379)

    def test_no_color(self):
        screen, out = self.render(500, no_color=True)
        self.assertEqual(screen.shape(), expected_mark(190, committed_path()))
        self.assertNotRegex(out, r"\x1b\[[0-9;]*(3[0-7]|4[0-7]|9[0-7])m")


# ── Progress ────────────────────────────────────────────────────────────────

@unittest.skipUnless(shutil.which("jq"), "the dashboard reads state with jq")
class SnakeProgressTest(unittest.TestCase):
    """A recorded-shape install with bursts, stalls and an unknown phase."""

    TOTAL = 900
    # (phase, index, packages landing, seconds passing, frames drawn)
    SCENARIO = [
        ("Starting installation", 0, 0, 1, 5),
        ("Preparing live environment", 0, 0, 40, 20),
        ("Preparing install target", 1, 0, 1800, 40),     # a 30-minute stall
        (PKG_PHASE, 2, 0, 600, 40),                       # no package lands for 10 min
        (PKG_PHASE, 2, 1, 1, 5),
        (PKG_PHASE, 2, 450, 1, 3),                        # a burst
        (PKG_PHASE, 2, 0, 900, 200),                      # stalled mid-phase
        (PKG_PHASE, 2, 460, 30, 400),
        (PKG_PHASE, 2, 50, 5, 20),                        # more than expected
        ("Configuring hibernation", 3, 0, 1, 10),
        ("Configuring system", 4, 0, 2400, 100),          # a slow DKMS build
        ("Configuring SSH access", 9, 0, 60, 10),         # not in the band table
        ("A phase renamed tomorrow", -1, 0, 600, 10),
        ("Creating factory snapshot", 13, 0, 5, 200),
        ("Installation complete", 13, 0, 1, 200, True),
    ]

    @classmethod
    def setUpClass(cls):
        cls.runs = cls.run_scenario()

    @classmethod
    def run_scenario(cls):
        with tempfile.TemporaryDirectory() as tmp:
            expected = Path(tmp, "expected")
            expected.write_text(f"{cls.TOTAL}\n")
            steps = "\n".join(
                "step '{}' {} {} {} {} {}".format(p, i, k, dt, f, 1 if rest else "")
                for p, i, k, dt, f, *rest in cls.SCENARIO
            )
            out = bash(PRELUDE + """
TGT="$TMPD/target"; mkdir -p "$TGT/var/lib/pacman/local"
NOW=1000; PHASE_T0=$NOW; PKG_LAST_CHANGE=$NOW; DASH_T0=$NOW; PK=0
step() {
  local extra="" k f
  [[ -n ${6:-} ]] && extra=',"finished_at":5'
  printf '{"target":"%s","total_phases":14,"current_index":%s,"current_phase":"%s"%s}' \\
    "$TGT" "$2" "$1" "$extra" >"$STATE"
  for (( k = 0; k < $3; k++ )); do mkdir -p "$TGT/var/lib/pacman/local/p$(( PK++ ))-1"; done
  NOW=$(( NOW + $4 ))
  install_progress
  snake_update_target
  echo "S"
  for (( f = 0; f < $5; f++ )); do
    snake_frame >/dev/null
    echo "F $WORK_PM $SNAKE_TARGET $SNAKE_SHOWN $POS"
  done
}
""" + steps, STATE=f"{tmp}/state.json", TMPD=tmp,
                OMARCHY_EXPECTED_PACKAGES_FILE=str(expected))
        runs, cur = [], None
        for line in out.splitlines():
            if line == "S":
                cur = []
                runs.append(cur)
            else:
                cur.append(tuple(map(int, line.split()[1:])))
        assert len(runs) == len(cls.SCENARIO)
        return runs

    def honest_bounds(self):
        """Upper bound on work done after each step, from the state alone."""
        bound, pkgs, bounds = 0, 0, []
        for phase, index, landed, _dt, _f, *finished in self.SCENARIO:
            pkgs += landed
            if finished:
                w = 1000
            elif phase in BANDS:
                lo, hi = BANDS[phase]
                w = lo
                if phase == PKG_PHASE:
                    w = lo + (hi - lo - 1) * min(pkgs, self.TOTAL) // self.TOTAL
            elif index >= 0:
                w = 10 + 990 * index // 14
            else:
                w = 0
            bound = max(bound, w)
            bounds.append(bound)
        return bounds

    def test_drawn_length_is_monotonic_and_never_overstates(self):
        runs = self.runs
        bounds = self.honest_bounds()
        shown_before = 0
        for (phase, *_), frames, bound in zip(self.SCENARIO, runs, bounds):
            done = phase == "Installation complete"
            limit = 1 + bound * 379 // 1000
            for work_pm, target, shown, _pos in frames:
                self.assertLessEqual(work_pm, bound, phase)
                self.assertLessEqual(shown, target, phase)
                self.assertLessEqual(shown, limit, phase)
                self.assertGreaterEqual(shown, shown_before, phase)
                self.assertLessEqual(shown - shown_before, 3, "at most 60 cells/s at 20 fps")
                if not done:
                    self.assertLess(shown, 380, "complete only after the installer exits")
                shown_before = shown
        self.assertEqual(runs[-1][-1][0], 1000)

    def test_time_alone_moves_the_bar_but_not_the_snake(self):
        runs = self.runs
        # 30 minutes in a phase with no work signal, 15 stalled mid-package
        # phase, 40 in a slow system configuration: the snake sits on the phase
        # floor or the package count, while the time-driven bar keeps moving.
        stalls = {
            2: BANDS["Preparing install target"][0],
            6: runs[5][-1][0],
            10: BANDS["Configuring system"][0],
        }
        for i, held in stalls.items():
            with self.subTest(step=self.SCENARIO[i][0]):
                before, during = runs[i - 1][-1], runs[i]
                self.assertEqual({f[0] for f in during}, {held})
                # Caught up after any burst, then still.
                self.assertEqual({f[2] for f in during[-20:]}, {1 + held * 379 // 1000})
                self.assertGreater(during[-1][3], before[3], "the time-driven bar still moves")

    def test_catches_up_and_holds(self):
        runs = self.runs
        # After the burst, the snake reaches the target and stays on it.
        work_pm, target, shown, _ = runs[6][-1]
        self.assertEqual(shown, target)
        self.assertEqual(target, 1 + work_pm * 379 // 1000)


# ── End to end ──────────────────────────────────────────────────────────────

FAKE_INSTALLER = r"""#!/bin/bash
set -u
state=$1 target=$2 dt=${FAKE_DT:-0.1}
mkdir -p "$target/var/lib/pacman/local"
write() {
  printf '{"started_at":1,"target":"%s","total_phases":14,"current_index":%s,"current_phase":"%s","phases":[%s]%s}\n' \
    "$target" "$2" "$1" "${3:-}" "${4:-}" >"$state.tmp" && mv "$state.tmp" "$state"
}
phases=("Preparing live environment" "Preparing install target" "Installing Arch + Omarchy"
  "Configuring hibernation" "Configuring system" "Staging provisioning" "Finalizing Limine boot"
  "Finalizing user" "Configuring login" "Configuring SSH access" "Configuring Tailscale"
  "Configuring DNS resolver" "Validating boot setup" "Creating factory snapshot")
write "Starting installation" 0
sleep "$dt"
for i in "${!phases[@]}"; do
  p=${phases[i]}
  write "$p" "$i"
  echo "› $p"
  if [[ ${FAKE_FAIL:-} == "$p" ]]; then
    write "$p" "$i" "{\"name\":\"$p\",\"status\":\"failed\",\"error\":\"simulated failure\"}"
    echo "error: simulated failure in $p" >&2
    exit 1
  fi
  if [[ $p == "Installing Arch + Omarchy" ]]; then
    for (( k = 0; k < 400; k++ )); do
      mkdir -p "$target/var/lib/pacman/local/pkg$k-1"
      (( k % 40 == 0 )) && sleep "$dt"
    done
  fi
  sleep "$dt"
done
write "Installation complete" 13 "" ',"finished_at":2'
"""


@unittest.skipUnless(shutil.which("script") and shutil.which("jq"), "needs script(1) and jq")
class SnakeDashboardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        installer = self.tmp / "installer"
        installer.write_text(FAKE_INSTALLER)
        installer.chmod(0o755)
        (self.tmp / "expected").write_text("400\n")

    def run_dashboard(self, rows=40, cols=120, env="", before="", background=False):
        t = self.tmp
        inner = (
            f"stty rows {rows} cols {cols}; {before} "
            f"OMARCHY_PATH='{t}/nologo' OMARCHY_SNAKE_PATH_FILE='{PATH_FILE}' "
            f"OMARCHY_EXPECTED_PACKAGES_FILE='{t}/expected' "
            f"OMARCHY_UI_INTERACTIVE=no OMARCHY_UI_AUTO_REBOOT=no OMARCHY_UI_FAILURE_ACTION=exit "
            f"OMARCHY_FAILURE_TAIL_LOG='{t}/install.log' {env} "
            f"'{DASHBOARD}' '{t}/install.log' '{t}/state.json' -- '{t}/installer' '{t}/state.json' '{t}/target'"
        )
        cmd = ["script", "-qefc", inner, str(t / "typescript")]
        if background:
            return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        return result.returncode

    def typescript(self):
        with open(self.tmp / "typescript", encoding="utf8", errors="replace", newline="") as f:
            return f.read()

    def replay(self, rows=40, cols=120, on_token=None):
        data = self.typescript()
        screen = Screen(rows, cols)
        screen.feed(data, on_token)
        return screen, data

    def snake_box(self, rows, cols):
        top = (rows - 19) // 2
        left = (cols - 30) // 2
        return top, left

    def test_success(self):
        self.assertEqual(self.run_dashboard(), 0)
        top, left = self.snake_box(40, 120)
        path = committed_path()
        counts, full = [], []

        def watch(screen):
            if "Installing Omarchy" in screen.text(top + 16, 0, 1):
                lit = halves(screen, top, left, 15, 30)
                counts.append(len(lit))
                if len(lit) == 380:
                    full.append(screen.shape(top, left, 15, 30))

        screen, data = self.replay(on_token=watch)
        self.assertTrue(counts, "the snake was drawn")
        # Non-decreasing, apart from a lone first cell blinking like a cursor.
        drops = [(a, b) for a, b in zip(counts, counts[1:]) if b < a and not (a == 1 and b == 0)]
        self.assertEqual(drops, [])
        self.assertEqual(full[-1], FULL_MARK)
        self.assertIn("\x1b[0;97m█", data, "the finished mark flashes")
        self.assertIn("Installed Omarchy in", screen.text())
        self.assertTrue(screen.cursor_visible)

    def test_failure_leaves_the_error_readable(self):
        self.assertNotEqual(self.run_dashboard(env="FAKE_FAIL='Configuring system'"), 0)
        top, left = self.snake_box(40, 120)
        path = committed_path()
        last = []

        def watch(screen):
            if "Installing Omarchy" in screen.text(top + 16, 0, 1):
                last[:] = [halves(screen, top, left, 15, 30)]

        screen, data = self.replay(on_token=watch)
        text = screen.text()
        self.assertIn("Omarchy installation stopped", text)
        self.assertIn("failed phase: Configuring system: simulated failure", text)
        self.assertIn("error: simulated failure in Configuring system", text)
        self.assertTrue(screen.cursor_visible)
        # Frozen no further than the failing phase's floor allows.
        self.assertLessEqual(len(last[0]), 1 + 720 * 379 // 1000)
        self.assertNotIn("\x1b[0;97m", data, "no finish flash on failure")

    def test_too_small_falls_back_to_the_bar(self):
        self.assertEqual(self.run_dashboard(rows=18, cols=100), 0)
        _, data = self.replay(18, 100)
        self.assertIn("░", data, "the existing bar is drawn")
        self.assertNotIn("\x1b[0;36m", data, "no snake head")

    def test_resize_recentres(self):
        self.assertEqual(
            self.run_dashboard(before="(trap '' TTOU; sleep 1.2; stty rows 30 cols 90 </dev/tty) &"), 0)
        data = self.typescript()
        # Replay only what was drawn after the snake was laid out at 30x90.
        top, left = self.snake_box(30, 90)
        screen = Screen(40, 120)
        states = []

        def watch(s):
            if "Installing Omarchy" in s.text(top + 16, 0, 1):
                states.append(s.text())

        screen.feed(data, watch)
        final = states[-1].split("\n")
        box = "\n".join(line[left:left + 30].rstrip() for line in final[top:top + 15])
        self.assertEqual(box, FULL_MARK)
        outside = [
            (y, line) for y, line in enumerate(final)
            if y not in range(top, top + 15) and re.search("[▀▄█]", line)
        ] + [
            (y, line) for y, line in enumerate(final[top:top + 15], top)
            if re.search("[▀▄█]", line[:left] + line[left + 30:])
        ]
        self.assertEqual(outside, [], "no stale snake left behind by the resize")

    def test_interrupt_restores_the_cursor(self):
        proc = self.run_dashboard(env="FAKE_DT=5", background=True)
        dashboard = None
        for _ in range(100):
            found = subprocess.run(["pgrep", "-f", f"^bash {DASHBOARD}|{DASHBOARD} "],
                                   capture_output=True, text=True).stdout.split()
            if found and (self.tmp / "state.json").exists():
                dashboard = int(found[0])
                break
            time.sleep(0.1)
        self.assertIsNotNone(dashboard, "dashboard started")
        time.sleep(1)
        os.kill(dashboard, signal.SIGINT)
        self.assertEqual(proc.wait(timeout=30), 130)
        screen, data = self.replay()
        self.assertTrue(data.rstrip().endswith("\x1b[?25h") or screen.cursor_visible)
        self.assertTrue(screen.cursor_visible)
        self.assertLess(data.rfind("\x1b[?25l"), data.rfind("\x1b[?25h"))


if __name__ == "__main__":
    unittest.main()
