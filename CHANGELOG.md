# Changelog

What changed for people who use the dechatter widget and command line. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Prepared sessions: a `setup.toml` can list fields to confirm on the machine under
  `[pending]`, each with a note (`"stock.height_mm" = "measure the top above the jaws"`). The
  session loads with the placeholder values, the widget shows how many values are to confirm,
  planning is refused naming them, and the setup wizard highlights each with its note; saving
  the wizard confirms them and writes the setup without `[pending]`. A zip holding only
  `<session>/setup.toml` imports as a new session.

## [0.7.0] - 2026-11-24

### Removed

- **Breaking:** the standalone command-line session commands (`dechatter init`, `plan`,
  `analyze`, `report`, `reset` and `materials`), audio-only pass segmentation without a widget
  run timeline, `soundfile`/`ffmpeg` decoding, and the recording-pause `M0` stops around
  facing in `program.nc`. Run sessions in the cncjs widget instead; `dechatter widget <folder>`
  still builds the static widget folder from source.

### Fixed

- Heavy, drifting or clipped chatter in steel can spread a tone across several Welch bins and
  flood the non-harmonic spectrum enough to hide the peak under the 27 dB prominence threshold;
  excess tonal energy within ±6 Hz of the peak is now summed back together and the reference
  floor's rise over the air dwell before the pass is capped at 20 dB.
- Spindle-speed estimation now scores runout harmonics alongside tooth passing and uses the
  air dwell before the pass to break ties when loud chatter drowns the cut's harmonics,
  improving recall on the Purdue dataset (88% chatter, 92% stable) and avoiding false `speed?`
  edge flags on chattering passes.
- The widget's stepper and run timeline now read `performance.now()` instead of `Date.now()`, so
  system or VM clock steps cannot record a negative cut window; a finished cut shorter than its
  planned lead-ins is marked "no cut heard" instead of "not run" so the stock face still moves.
- Closing the microphone clears its open state before `AudioContext.close()` settles, and a
  failed write in the worker returns its refusal even if the follow-up filesystem sync fails.
- Resetting a session logs a warning when its `.done` marker cannot be written and gives
  widget-actionable recovery instructions (`export, delete and import`) if the fallback rename
  fails too.

## [0.6.0] - 2026-11-23

### Added

- **Stepping the depth**: a session can hold the radial width and step the axial depth
  instead of the reverse. Choose "the axial depth, at a set width" in the wizard's Cut step
  (`vary = "depth"`, `radial_width_mm` and `depth_max_mm` in `[cut]`). A width equal to the
  cutter diameter cuts full-width slots into the top, each in a row of its own with
  `slot_wall_mm` of stock (`[program]`, a quarter of the cutter by default) on both sides,
  across `thickness_mm`; a narrower width mills the front face at that width. The map, the
  results, the report and the G-code comments show depth against speed. `other_widths_mm`
  compares narrower widths the way `other_depths_mm` compares shallower depths. No cut is
  deeper than `depth_max_mm`, and with `height_mm` given the deepest must stay
  `clearance_mm` above it, on a faced-down top too. The slot wall cannot change once a round
  was cut, and a slot stopped part-way closes its row to the lanes after it.
- A round that steps the depth runs **deepest first**, on each face and from lane to lane,
  because stock is clear only as deep as the shallowest cut made before it. Its first pass
  is its heaviest: there is no ramp up to it, so be ready to feed-hold from the start. Slots
  in a single lane keep the ramp, shallowest first. The program, `dechatter plan` and the
  widget say which order a round runs in. A round with more cuts than its stock holds
  leaves out the heaviest, as a ramp does.

### Changed

- A deeper cut may go to a fresh lane wherever the lane before it was cut at least as deep
  that far back from Y0. Before, any cut deeper than a block's shallowest needed fresh stock
  or the top faced down.
- The widget's session state names the stepped dimension rather than the width
  (`engagement_mm`, `level_mm`), for anything that reads it.
- A round uses the stock left in every lane before the top is faced down. It fills what
  is left of the lane it starts on, then goes on into the next lane and the ones after it,
  each a fresh face at Y0 with its own truing pass, within the same program; only once
  every lane is used (and `height_mm` is given) does it face the top down, partway through
  if need be, and go on at the first lane below the new top. It faces at most once: the
  cuts the new top's lanes cannot hold are dropped, and the next round plans afresh.
  Before, a round that did not fit what its lane had left or the single next lane faced
  the whole top down, wasting those lanes and every lane after them. A chip-load sweep may
  now span lanes, still only whole. The program comments each lane change and the facing;
  `dechatter plan`, the widget and the report list the lanes a round cuts, the pass each
  starts at, and where the top is faced. The next round starts in the lane the last one
  ended in.
- From the command line, a program that faces the top down partway stops (`M0`) before
  and after the facing: pause the recording through it, so the cuts found by their sound
  follow on as planned. A recording that runs on through a facing that ran to its end is
  still matched: the facing is heard as one long stretch and left out, and a few rows of a
  facing stopped early no longer make the cuts before them go unmatched. The widget records
  on through it and leaves it out of the analysis.
- Once a run gets to facing the top down partway, the next round starts from what the
  facing did: on the new top if it finished, where the run stopped if it never started, and
  if it stopped part-way, by facing the top down again from the old top before any cut. The
  widget records the facing's rows and knows which. A recording cannot tell (a facing
  stopped after a few rows sounds like cuts), so whenever it shows the run got to the facing
  `dechatter analyze` asks for `--facing done` (a round run to its end), `cut-short` or
  `not-run` (or `"facing"` in the round's `results.json`), and `dechatter plan` waits for
  it; `cut-short` and `not-run` count what was heard after the facing as not run (no
  verdict can be set on it), set in `results.json` by hand as well. `results.json` keeps
  what was heard, so correcting `"facing"` to `done` brings it back. `dechatter plan` and
  the program's header say so up front. Sound after the last cut before the facing is no
  longer taken for the next pass stopped early.
- Sessions planned by this version cannot be opened by dechatter 0.5.0, whose plans hold a
  round on a single lane. Sessions from 0.2.0 to 0.5.0 load as they are.

### Fixed

- A pass the widget's run cut, but whose stretch of the recording is silent or cut off (the
  recording stopped before the last passes), now says "no cut heard", with no verdict, and
  the next round still starts behind its face. Before, a silent stretch was analysed to a
  prominence of NaN, which made the round's results unreadable: the widget said
  "Unexpected token 'N' … is not valid JSON". Results and widget replies now refuse NaN and
  infinity outright. A session 0.5.0 saved NaN into opens again: those numbers are dropped,
  and with a NaN prominence the detector's verdict too. `dechatter analyze` can analyse the
  round again.
- Lead-in and cut times of a cut wider than half the cutter were computed as if it met the
  stock at the cutter's centre line. Only run-time estimates and where a cut is looked for
  in a recording were affected.

## [0.5.0] - 2026-11-23

### Added

- **Facing the top down**: given the stock's height above the vise jaws (`height_mm` in
  `[stock]`, the wizard's Height above the jaws), dechatter faces the top down itself once
  every lane is used, or before a deeper depth on a face cut only shallower, and starts again
  at the first lane with the same zero. The round's program takes the top down below the
  deepest cut first, over the stock's thickness (`thickness_mm`, or the width used), in
  levels and rows set by `face_depth_mm` and `face_width_mm` in `[program]`, then stops for
  the recording. The widget, the program, `dechatter plan` and the report say so. Without a
  height, nothing changes: fresh stock is asked for.
- A pass that sounds no louder than the spindle in the air before it (under 1 dB over it)
  and would be called stable now says "no cut heard", with no verdict: it may have cut
  nothing, as when fresh stock is picked without mounting any. A verdict can be set by hand.

### Changed

- Sessions planned by this version cannot be opened by dechatter 0.4.0, whose plans lack the
  faced top. Sessions from 0.2.0 to 0.4.0 load as they are.

### Fixed

- A chip-load sweep is planned only where all its cuts fit. One cut short by the stock left
  its heaviest chip loads untried and reported the last one tried, below the session's own
  chip load, as the limit. A sweep whose every chip load stayed stable now says the limit lies
  above them ("stable at every chip load tried, up to"), sessions already cut included.

## [0.4.0] - 2026-11-23

### Added

- **Lanes**: a long block is split into lanes along X. When a lane's face runs out of
  width, the next round moves on to the next lane, a fresh face at Y0 at the same height
  above the vise jaws, without remounting or re-zeroing. Each pass cuts one lane and stops
  just short of the next; a lane is never cut back further than the one before it. By
  default the number of lanes is worked out from the stock length: as many as leave every
  cut at least 6 s long at the fastest test speed (`min_cut_s` in `[planner]`, the
  wizard's Shortest cut to listen to), and 1 on a short block, as before. The wizard shows
  the number; `lanes` in `[stock]` (Lanes along X, under Advanced) sets it by hand. The
  session's first round settles it. The widget, the program, `dechatter plan` and the
  report say which lane a round cuts.

### Changed

- Sessions planned by this version cannot be opened by dechatter 0.3.0, whose plans lack the
  lane. Sessions from 0.2.0 and 0.3.0 load as they are and keep cutting the whole length.

## [0.3.0] - 2026-11-12

### Changed

- The widget runs a round itself: press Start in the widget instead of loading the program
  into cncjs and pressing play. It sends one move at a time through cncjs and waits for the
  controller to report Idle at its end, so it knows exactly when each cut starts and ends
  and records those windows with the sound. Hold, Resume and Stop are in the widget; Stop
  always holds first and resets the controller only once it has reported the hold complete
  (or Idle) and nothing has changed for a second; if the connection drops meanwhile, nothing
  is reset, then or on reconnect. An alarm (its state or an `ALARM:` line) ends the round with
  nothing more sent and is left for you to clear (the widget never unlocks). With a
  manual spindle no `M0` is sent: the widget asks for each speed and waits for Continue, so
  the "set the spindle" prompt shows only once the machine has stopped. Start is refused
  while cncjs runs a job of its own, in alarm or hold, or before the machine's units are
  known, and a job cncjs starts during the round stops it. If the machine refuses, or the
  microphone closes, while the round is being prepared, it does not start and no recording
  is kept. While a round runs,
  or starts, its session stays open: the session list, New, Import and Setup wait for it to
  end.
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
  not run". A widget run that ended before its first cut leaves the round to run again,
  and a cut stopped before the cutter reached the stock counts as not run. A hold before
  the cutter reaches the stock no longer puts air into the cut analysed. Silence in a
  recording, even most of it (a recorder left running after the round), short pauses with
  the spindle off, or silence broken up by knocks, is told from a quiet spindle under loud
  cuts, and no longer hides the spindle's level.
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

[Unreleased]: https://github.com/pedropaulovc/dechatter/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/pedropaulovc/dechatter/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/pedropaulovc/dechatter/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/pedropaulovc/dechatter/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/pedropaulovc/dechatter/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/pedropaulovc/dechatter/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/pedropaulovc/dechatter/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/pedropaulovc/dechatter/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/pedropaulovc/dechatter/releases/tag/v0.1.0
