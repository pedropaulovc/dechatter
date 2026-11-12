# Changelog

What changed for people who use the dechatter widget and command line. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- The widget runs a round itself: press Start in the widget instead of loading the program
  into cncjs and pressing play. It sends one move at a time through cncjs and waits for the
  controller to report Idle at its end, so it knows exactly when each cut starts and ends
  and records those windows with the sound. Hold, Resume and Stop are in the widget; Stop
  resets the controller only once its hold is complete, and an alarm is left for you to clear
  (the widget never unlocks). With a
  manual spindle no `M0` is sent: the widget asks for each speed and waits for Continue, so
  the "set the spindle" prompt shows only once the machine has stopped. Start is refused
  while cncjs runs a job of its own, in alarm or hold, or before the machine's units are
  known, and a job cncjs starts during the round stops it.
  `program.nc` is still written, unchanged, for the command line.

- On a machine in G20 the whole widget shows inches, not only the wizard and the programs:
  the session summary, round headings and pass table, the front face position, the best
  cuts, the chip-load sweep result and the stability map's axes. Inches have 4 decimals
  (chip loads 5), with millimetres alongside where there is room. Files stay in mm.

### Added

- **Reset** next to the widget's session list, and `dechatter reset <session>` (with
  `--yes` to skip the question), discard every round of a session and keep its setup. The
  session starts again from round 1 and its setup can be changed freely, for instance after
  a run in the air to check the toolpath, or one with Z zero set wrong. It asks first,
  naming the rounds and recordings lost, offers Export first, and is refused while the
  session records or a cncjs job of its program runs, and for a session whose setup does
  not load (delete that one instead); rounds that do not load are no obstacle. A stale `report.html` in the session goes too.
- **Delete** next to the widget's session list removes a session from the browser for good,
  after a confirmation that names its rounds and recordings and offers Export first. It
  works for sessions that no longer load, such as ones from dechatter 0.1.x. One whose setup
  does not load cannot be exported, and the confirmation says so instead. It is refused while the session
  records or a cncjs job of its program runs.

### Fixed

- Cuts were matched to the wrong passes, seen on a real manual-spindle session: light cuts at
  a low speed fell under one level threshold for the whole recording, and the rest slid onto
  the wrong passes; passes never run had cuts forced onto them. A round the widget runs is
  now analysed from the cut and dwell windows it recorded. A recording without them is
  searched against the spindle's own noise around each cut, keeps the pass order with the
  narrowest cuts taken as the unheard ones, and marks passes after the last cut as "likely
  not run". A widget run that ended before its first cut leaves the round to run again.
  A pass whose spindle speed was found at the edge of its search is flagged.
- A recording could get stuck with no way to stop it, and Export was disabled meanwhile. The
  recording now follows the widget's own run, which always ends (Stop, the last pass, an
  error or a lost connection) and keeps the cuts made. A run held for a minute, or a machine
  idle but not where its move ends, points to Stop. Export works during a run, without the
  round in progress, and says so.

## [0.2.0] - 2026-11-12

### Changed

- **Breaking:** the spindle motor power is given in horsepower. `spindle_power_w` in
  `[machine]` is now `spindle_power_hp` (1 hp = 745.7 W), and the wizard asks for hp.
- **Breaking:** sessions from 0.1.x do not load in 0.2.0, in the widget or the command line:
  their setup, plans and results are in the older format. Start a new session for each
  tool. dechatter refuses them with a message that says so: a 0.1.x setup (it has
  `spindle_power_w`) is named as one, and a round in the older format says it was written
  by an older dechatter. A round damaged otherwise is named with its file and the problem.
- **Breaking:** once a round has been cut (recorded or analysed), the setup can no longer
  change in anything that shapes the cut or its sound: the machine's weight and spindle
  control, the tool, the work material, depth, direction and chip load, the stock length,
  the dwell and the coolant. The wizard and `dechatter plan` refuse such a change and say
  which fields differ. Start a new session for the new setup.
- Programs are written in the machine's units: on a machine in G20 the program is in
  inches, so a job never switches the machine's units, even one stopped part-way. Comments,
  plans and results stay in millimetres. "Load into cncjs" waits until cncjs reports G20 or
  G21; until it does, you can say which the machine is in.
- A round planned but not yet run is discarded when the setup changes its program, and the
  widget says so. Plan it again. Program names now carry a short code, so a program loaded
  into cncjs before such a change, or before a widget reload, is not taken for the new one:
  load it again before running it.
- The machine's speed range and power, the cut's speed and width limits and shallower
  depths, the machine's name, the safe height, clearance, plunge feed, truing width, stock
  width and the planner's settings may change at any time. Cuts outside narrowed limits stay
  in their round but are not recommended, nor are cuts over a lowered power cap, and the
  chip-load sweep is offered only for a cut the power cap leaves room to sweep. A sweep's
  result stays within the power cap as it is now. On the map, a stable cut wider than a narrowed
  width limit counts as stable at the widest width searched; chatter that wide, and cuts at
  speeds outside the limits, are left out. Cuts at the edges of the search, such as a width
  the grid rounds just above the limit, or speeds a coarser speed step no longer reaches,
  keep counting.
- The setup wizard shows on each step only what has to be answered about the machine, tool
  and stock. Settings with a default or a computed value sit in a collapsed Advanced section,
  showing what an empty entry becomes; a section holding a changed setting opens by itself.
- The setup cannot be saved while a job runs in cncjs or while a round is being recorded.

### Added

- A chip-load sweep after convergence: the widget offers it, and `dechatter plan --sweep`
  plans it. The best stable cut runs again at five chip loads, by default 0.6 to 1.7 times
  the session's, lightest first and inside the power cap. The result, the highest chip load
  that stayed stable with its feed, shows beside the best cuts, in `dechatter report` and in
  the printed report. Its cuts do not change the stability map. The bounds are two new
  planner settings, `chip_load_sweep_low` and `chip_load_sweep_high`.
- A printable report: one HTML page for Letter or A4 with the recommended cuts per depth
  (feed, removal rate, spindle power), the stability map, every round's passes with
  verdicts set by hand marked, and the setup. The widget offers it on convergence and beside
  the best cuts, exports include it as `report.html`, and `dechatter report <session>
  --html <file> [--units in]` writes it. An export still succeeds, without the report, if
  the report cannot be made.
- An (i) button on every wizard field with a longer explanation: what it changes, its
  default, and whether it may change after a cut.

### Removed

- The "Use mm instead" button and the `G20` line at the end of programs: programs no longer
  change the machine's units, so neither is needed.

### Fixed

- A new session could plan round 1 from the example setup when the wizard was cancelled.
  Planning now waits until the setup is filled in, and the widget points to the wizard.
- Saving a new setup after rounds existed kept rounds planned for the old one.

## [0.1.1] - 2026-11-10

### Changed

- No changes to the widget or the command line. The first release published by the release
  workflow, with npm provenance.

## [0.1.0] - 2026-11-10

### Added

- Command line: `dechatter init`, `plan`, `analyze`, `report`, `materials` and `widget`.
  Each round of test cuts is a G-code program; a recording of the run is split into passes
  and each is classified as stable or chatter from its sound.
- A Bayesian stability map over spindle speed and radial width that chooses the next cuts,
  and stops when no cut promises enough extra removal rate.
- Shallower depths to compare once the deepest one is settled.
- Manual spindle control: the program stops before each speed for you to set it.
- The cncjs widget: runs a whole session in the browser (Pyodide), records with the
  browser's microphone while the job runs, loads programs into cncjs, and shows verdicts,
  the stability map and the best cuts. A setup wizard follows the machine's G20/G21 units.
  Sessions export to a zip the command line reads, and import in another browser.
- Published on npm as `dechatter`, with third-party notices for the bundled Pyodide.

[Unreleased]: https://github.com/pedropaulovc/dechatter/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/pedropaulovc/dechatter/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/pedropaulovc/dechatter/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/pedropaulovc/dechatter/releases/tag/v0.1.0
