# Installer snake

The path is generated during development with `python3 assets/install-snake/generate.py`.
The ISO reads the committed 380-line path and uses Bash/ANSI only. No video,
image conversion, Python generator, or new package is needed at install time.

The coarse grid was checked against the official site SVG
(`omacom/omarchy-site`, `brand/omarchy-logo.svg`, blob
`aa239e1f0402c3b22be01c9484f4eecedad2935e`) at 1/80 scale and at full-size cell
centres. All 95 cells match. Coordinates are zero-based: the mouth is (14,8),
and (13,7) has two neighbours, despite the original brief calling it a dead end.

A heading-first DFS makes a spanning tree of the coarse grid. Each coarse cell
becomes a four-cell cycle; merging facing edges along the tree produces one
Hamiltonian cycle on the doubled grid. Its 380 cells cover the logo exactly,
with no gaps, overlaps or duplicated cells.

## Progress and rendering

The existing phase bands are retained as approximate work weights: the package
phase spans 6.5–70%, configuration spans 72–85%, and boot finalization spans
85.5–95%; smaller phases fill the remaining bands. Entering the next phase
credits the previous one. During package installation only the actual target
pacman database count advances progress, divided by the build's resolved
package count and capped below the next phase boundary. These weights describe
work, not elapsed time or a prediction of time remaining. With no valid count,
progress waits at the phase boundary. Unknown phases hold the previous value.
The existing atomic state file and log writers are unchanged.

Length is `1 + floor(progress_per_mille * 379 / 1000)`, capped at six new cells
per 100ms-or-longer frame. Progress is monotonic and has no time-based bonus.
A stalled head blinks without extending the snake. State is sampled at 2Hz;
rendering runs at no more than 10Hz and only changed logo rows are written.
100% requires the actual installer's successful exit, not merely `finished_at`.
The full logo then flashes briefly and remains above the existing reboot prompt.

The Linux VT uses its existing palette, including colour 2 (`#9ece6a`). The
loaded console Unicode map must contain U+2580, U+2584 and U+2588; missing or
unreadable coverage keeps the old wordmark/bar, or a compact percentage/stage
line when the wordmark will not fit. UTF-8 terminal emulators use half-blocks.
`NO_COLOR` suppresses colour. A terminal below 40 columns or 22 rows starts with
plain log output. No TTY or TERM=dumb also streams logs without escapes. A live
resize into a small terminal switches to one compact status line and can grow
back into the snake. On failure, a tall console keeps the frozen snake; 80x25
reserves the space for the error and existing log actions. Unattended success
still reboots, unless OMARCHY_UI_AUTO_REBOOT=no.

## Evidence and limits

Run `bash test/unit/install-snake-test.sh` for path assertions, renderer samples
at p=0, 1/379, 0.5 and 1, bounded monotone growth, stalled work, success gating,
80x25 and large-console PTY runs, forced failure, Ctrl-C, NO_COLOR, small-console
and non-TTY output, live resize, all 380 incremental screens, and synthetic font-map dispatch. These are simulated
installer children, not actual disk installations. `bin/omarchy-iso-installer`
previews the real renderer using simulated installation progress.

Before an upstream PR is ready, build with
`./bin/omarchy-iso-make --local-source <omarchy-checkout> <pkgs-checkout> --keep-pkg-cache --no-boot-offer`
and run `./bin/omarchy-iso-test <iso> --install-only --no-preview` on an Arch/Omarchy
host with Docker and KVM. Capture early/50%/100% and forced-failure QMP screens;
confirm the loaded ISO font and all three block glyphs. The implementation
session lacked Docker, QEMU, KVM, an ISO and the package-recipe checkout, so
none of those real-VM results are claimed. No upstream PR has been submitted.
