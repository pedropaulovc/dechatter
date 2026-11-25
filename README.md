# dechatter

dechatter finds the fastest chatter-free cut for one tool on one milling machine. It writes a
G-code program of test cuts, listens while the machine runs it, and decides from the sound
which cuts chattered. From that it either plans a finer round of cuts or reports the best
stable speeds, widths and depths.

It runs as a widget inside [cncjs](https://cnc.js.org), entirely in the browser and recording
with that computer's microphone. The G-code is
plain Grbl dialect (`G0`, `G1`, `G4`, `M3`, `M5`), written for FluidNC and usable on Grbl and
grblHAL.

## What it measures

Chatter depends on the machine, the holder, the cutter and how far the cutter sticks out.
Machine weight, spindle power and material cannot predict it. They only bound the search:
the speed range, the power cap and the starting chip load. The cuts are the measurement, and
a result applies to that one tool assembly. Change the stickout and you start a new session.

Each round holds the chip load and one dimension of the cut constant and varies the spindle
speed and the other, one pass per combination. By default it holds the axial depth and steps
the radial width, cutting along the front face of a block held in the vise. It can hold the
radial width and step the axial depth instead, which is how a full-width slot is measured (see
[Stepping the depth](#stepping-the-depth-slots-and-set-widths)). Feed follows speed so the
chip load stays put.

### Comparing depths

A deep, narrow cut and a shallower, wider one can remove the same amount, and only cutting
tells which your machine prefers. List shallower depths in the wizard's Cut step (or
`other_depths_mm` in `setup.toml`) and the session continues with them once the deepest depth
is settled, deepest first. A width that was stable in a deeper cut is taken as stable in a
shallower one, so each further depth starts from those widths and only looks for how much
wider it can go. A depth is left once no cut within the power cap that would beat the best
one found keeps a fair chance of being stable. The results list the best cuts of all depths
together.

The depths run deepest first because a shallower cut leaves the face flat only above its own
depth. A deeper cut is never planned on such a face. It needs a fresh lane where the lane
before it was cut at least as deep, fresh stock, or the top faced down (see
[Facing the top down](#facing-the-top-down)).

### Stepping the depth: slots and set widths

Some cuts have their width fixed. A full-width slot is as wide as the cutter, and the open
question is how deep it can go. Choose "the axial depth, at a set width" in the wizard's Cut
step (`vary = "depth"` in `[cut]`), give the width (`radial_width_mm`) and the deepest cut
to try (`depth_max_mm`), and the session holds that width and steps the spindle speed and
the depth. The choice is part of what a session measures, so it cannot change once a round
was cut. The map, the results and the report then show depth against speed. Removal rate is
still width × depth × feed, the power cap applies as before, and the chip load is the
session's at every width: dechatter does not thin the chip for the width. Round 1 cuts each
speed at `round1_depth_fractions` of the deepest cut (20%, 40% and 60%), and the search moves
in steps of `depth_step_mm`, both in `[planner]`. Left out, the step is a round number that
gives at least 50 depths up to the deepest (0.01 mm for 0.75 mm, 0.1 mm for 6 mm): the
planner only steps up where the next depth is likely stable given the one below, and with
few, coarse steps the deepest ones would each be too big a step to take. No depth searched
is deeper than `depth_max_mm`, which a step that does not divide it stops short of. Given
`height_mm`, it must leave the clearance (`clearance_mm`) below `depth_max_mm`, and on a top
that was faced down no cut is planned whose bottom would come within that clearance of it:
such a cut waits for fresh stock.

A width equal to the cutter diameter cuts **slots into the top**. Each slot runs along X
from the air beyond the end of the stock, where the cutter plunges and the spindle is heard
before the cut, and has a row along Y to itself. `slot_wall_mm` of stock (in `[program]`, the
wizard's Wall beside each slot, a quarter of the cutter diameter by default) stands before
the first slot, between slots and behind the last, so every slot is cut from solid stock at
the cutter's full width. The rows go across the stock's thickness (`thickness_mm`, or
`width_mm` without it), which must hold at least one slot and its two walls. No face is
trued. With lanes, a slot in a later lane starts inside the earlier lane's slot in its row,
so it may be no deeper than that one, and that one must have run its lane's length: a slot
stopped part-way leaves its row closed to the lanes after it. The wall sets the rows, so it
cannot change once a round was cut. Once the rows are used in every lane, the top is faced
down below the deepest slot (given `height_mm`), or fresh stock is asked for, as in side
milling.

A narrower width **mills the front face**, as a session that steps the width does: every
test cut takes the set width at a depth of its own, and each face is trued as deep as its
deepest test.

Stock is clear only as deep as it was cut: a face only above the shallowest cut made on it,
and a lane only as deep as the lane before it was cut that far back, since the cutter comes
in through that lane. A shallower cut can follow a deeper one there, never the reverse. So a
round that steps the depth runs **deepest first**, on each face and from lane to lane. Cuts
no deeper than the face the round starts on come first, to use it up; deeper ones go to the
next lane where it is clear for them, else to a faced top or fresh stock. This costs the
ramp. In a session that steps the width, every cut is a small step up from a lighter one
that ran before it, and you can stop when chatter turns violent with the heaviest cuts still
ahead. Deepest first, the round's heaviest cut is its first, made on the planner's odds
alone (at least 50% likely stable on what was measured; nothing was measured before round
1), and the cuts after it are lighter. Be ready to feed-hold from the first pass, and start
with a modest `depth_max_mm` or cautious `round1_depth_fractions`. With a manual spindle
the speeds no longer come grouped: the round stops wherever the speed changes. The other
way to keep the ramp, facing the top before every deeper cut, would spend most of a round
facing. Only slots in a single lane (`lanes = 1`) keep it, since each is cut into stock of
its own: they run shallowest first, and a deeper slot can follow in the next row of the
same top. When a deepest-first round has more cuts than the stock it can reach, the
heaviest ones wait for the next round, as they do at the end of a ramp.

`other_widths_mm` (the wizard's Narrower widths to compare) is to widths what
`other_depths_mm` is to depths. Once the widest width is settled the session goes on with
the narrower ones, widest first. A depth that was stable in a wider cut is taken as stable
in a narrower one at that speed, so each width starts from those depths and only looks for
how much deeper it can go. A width is left once no cut within the power cap that would beat
the best one found keeps a fair chance of being stable, and the results rank the best cuts
of all widths together. Going from slots to a narrower width, the front face would be milled
under a top that holds slots: the top is faced down first, or fresh stock asked for.

## Use in cncjs

```sh
npm install dechatter
cncjs --mount "/dechatter:$(npm root)/dechatter/web"   # PowerShell or a POSIX shell
```

The npm package is the widget as a static folder, about 30 MB. To build that folder from
source instead:

```sh
uv tool install git+https://github.com/pedropaulovc/dechatter

dechatter widget ~/dechatter-widget
cncjs --mount /dechatter:$HOME/dechatter-widget
```

In cncjs, open Manage Widgets, enable the Custom widget, and set its URL to `/dechatter/` in
the widget's settings. Open cncjs at `http://localhost:8000` or over HTTPS: browsers only give
a page the microphone on those. Then, in the widget:

1. Create a session. A short wizard asks for the machine, the tool, the cut and the stock. It
   shows lengths in inches when the machine is in G20 and in millimetres when it is in G21,
   or with no machine connected; so does the rest of the widget. Its last step holds the planner's settings, which rarely
   need changing. Settings with a sensible default or a computed value (the machine's name,
   the holder, the cut's limits and chip load, the program's dwell, clearance, plunge feed,
   truing width and coolant, and the planner) sit in a collapsed Advanced section of their
   step, showing what an empty entry becomes. A section holding a changed setting opens by
   itself. Every field has an (i) button with a longer explanation: what it changes, its
   default, and whether it may change after a cut. Saving from the wizard rewrites the
   session's setup file without comments.
   Programs are written in the machine's units too (G20 or G21, as cncjs reports it), so a
   job never changes them, even one stopped part-way. Their comments, like the plan and the
   results, stay in millimetres.
   Nothing is planned until the wizard has been saved: a new session holds only an example
   setup. Each round keeps a copy of the setup it was planned from. Once a round has been
   cut, the setup can no longer change in anything that shapes the cut or its sound (the
   machine's weight and spindle control, the tool, the work material, depth, direction and
   chip load, the stock length and lanes, the dwell and the coolant), since the stability map belongs
   to the setup it was measured with: start a new session instead. What only bounds the
   search may still change (the machine's speed range and power, the cut's speed and width
   limits and shallower depths), as may the machine's name, the safe height, clearance,
   plunge feed, truing width, stock width and the planner's settings. Cuts outside narrowed
   bounds stay in their round but are not recommended, nor are cuts over a lowered power
   cap. On the map, a stable cut wider than a narrowed width limit counts as stable at the
   widest width searched; chatter that wide, and cuts at speeds outside the limits, are left
   out. A round
   planned but not yet run is discarded when the setup changes its program, so the next
   plan uses the new setup.
2. Plan a round. Round 1 is a coarse grid of speeds and widths.
3. Set the work zero at the left end of the stock, on its front face, at the top.
4. Press Start. The widget runs the round itself through cncjs, one move at a time: it
   sends a move, waits until the controller reports Idle at its end, then sends the next.
   So it knows when every cut starts and ends, and records those times with the sound; at
   most one move is ever queued, so if the tab closes or the connection drops the machine
   finishes that move and stands. Hold, Resume and Stop are in the widget (cncjs's own feed
   hold works too). Stop holds, waits until the controller reports the hold complete (or
   Idle) and nothing changes for a second, then resets it; if the hold never completes, or
   the connection drops meanwhile, it does not reset, and says so. An alarm stops the round with nothing more sent and is left for you
   to clear in cncjs (home, or unlock): the widget never unlocks. While a round runs, its
   session stays open: the session list, New, Import and Setup wait for it to end. Start is refused while cncjs runs a job of its own, while the machine is in
   alarm or held, or before its G20/G21 mode is known, and a job cncjs starts during the
   round stops the round. To check the toolpath first, raise Z zero and run the round in the
   air, then Reset the session (next to the session list) and set Z zero back on the top of
   the stock.
5. Recording starts with Start and stops when the last pass ends, or on Stop. The widget
   then shows each pass as stable or chatter, the stability map and the best cuts so far.
6. If your ears disagree with a verdict, change it in the table. Plan the next round and
   repeat until the widget reports convergence.
7. Optionally, plan the chip-load sweep it then offers: the best cut once more at five
   chip loads, from 0.6 to 1.7 times the session's (both Advanced planner settings), lightest
   first and inside the power cap. The result is the highest chip load that stayed stable,
   with its feed. Its cuts do not change the map, which stays at the session's chip load.
8. Download the report: one HTML page to print (Letter or A4, black and white) and keep with
   the tool. It holds the recommended cuts per depth with feed, removal rate and spindle
   power, the stability map, every round's passes (marking verdicts set by hand) and the
   setup. Lengths follow the machine's G20/G21, with mm beside. It is offered on convergence
   and any time beside the best cuts.

Everything runs in the browser tab. Pyodide runs `dechatter`'s Python engine in a worker,
which `dechatter widget` downloads once and copies into
the folder, so the shop PC needs no Python and no internet. The microphone is the browser's,
on whichever computer shows cncjs, opened with echo cancellation, noise suppression and
automatic gain switched off. The widget shows its level before any cutting and warns if the
browser kept any of that processing on.

Sessions are stored in that browser (IndexedDB), per cncjs address. Export saves one as a zip
holding `setup.toml` and each round's plan, program, `recording.wav`, results and the job
timeline of the run (each pass's cut and dwell windows), plus `report.html`. Import loads it in another browser. Keep the tab open while a round runs: the recording lives in
the page until the round ends. Export works during a run too, without that round. If a run
seems stuck (held for a minute, or the machine idle but not where its move ends), the widget
says so and points to Stop, which keeps and analyses the cuts made so far. Reset, next to the session list, discards every round and
keeps the setup, so the session starts again from round 1 and its setup can be changed
freely; it needs a setup that loads,
though its rounds need not. Delete removes a session for good after asking, and offers
Export first. It also removes sessions that no longer load, such as ones from dechatter
0.1.x; one whose setup does not load cannot be exported.
Both are refused while that session records or its job runs.

### Prepared sessions

A session can be prepared ahead of time by someone who is not at the machine: write its
`setup.toml`, zip it as `<session>/setup.toml` (a folder named after the session holding the
file) and hand the zip over; the operator imports it. Values only the machine can tell, such
as the stock's size and height above the jaws or the tool's actual stickout, are listed under
`[pending]` with a note saying how to check each, while their fields hold a placeholder:

```toml
[pending]
"stock.height_mm" = "measure the top above the jaws (calipers)"
"tool.stickout_mm" = "measure from the collet face after the tool setter"
```

Each key is a field the wizard shows, as `section.field`. The widget shows how many values are
to confirm on the machine and plans nothing until they are: the setup wizard highlights each
one with its note, opening the Advanced section that holds it, and saving the wizard writes
the setup without `[pending]`. Export keeps the table while it is there.

### Manual spindle

If the spindle is started and set by hand, choose "me, by hand" on the wizard's first step
(`spindle = "manual"` under `[machine]` in a setup file). The program then never commands the
spindle. The round stops before each speed with the cutter clear of the work
and shows the speed to set: set it and press Continue. Cuts are grouped by speed, lightest first, so a round has one stop per
speed: five in round 1. The first stop is where you start the spindle, and you stop it yourself
after the round ends.

Set the speed within 5% of what is asked; the detector finds the actual speed inside that
range. The stability map is built on the requested speeds, so a dial that reads off by a
constant amount shifts the whole map by that amount.

Round 1 is a coarse grid of speeds and widths (or depths, in a session that steps the
depth). Later rounds are chosen from what was measured. A program that steps the width
orders its passes from the narrowest cut to the widest; one that steps the depth runs
deepest first, except slots in a single lane. A program that mills the front face starts
each face with a light truing pass, and every program tells you where the face will end up.

## Safety

The program looks for chatter on purpose, so later passes are heavier than earlier ones and
some of them will chatter. A round that steps the depth on a face or across lanes runs the
other way, deepest first: its first pass is its heaviest (see
[Stepping the depth](#stepping-the-depth-slots-and-set-widths)). Press Stop (or feed-hold) when a
cut gets violent: the passes that ran still count, the aborted
one is marked as such, and the next round starts by clearing the step it left on the face
(an aborted slot just leaves its row used).

Every planned cut stays under a power cap (60% of rated spindle power by default) and inside
the material's surface-speed limit. The built-in cutting data is a
conservative starting point. Override `chip_load_mm` and the speed range when you know better.

## How a pass is classified

Forced vibration sits on harmonics of the spindle rotation. Chatter is a strong tone anywhere
else. For each pass dechatter estimates the true spindle speed from the tooth-passing and
runout harmonics (using the dwell in air before the pass to break ties when loud chatter drowns
them), removes every spindle harmonic from the spectrum, and takes the strongest peak
left over. The pass is unstable when that peak (including excess energy drifting within ±6 Hz across the
pass) is at least 27 dB above the median of the remaining spectrum (capped at 20 dB over the air
dwell's floor, so heavy or clipped chatter cannot hide itself by raising the floor) and at least 10 dB above the same frequency in the dwell just before the
pass, when the spindle was turning in air. The second test rejects tones the machine makes
without cutting, such as drive whine.

A pass whose cut is less than 1 dB louder than that dwell, and which would be called stable,
says "no cut heard" instead, with no verdict. The cuts of real sessions sounded 1 to 20 dB
over the spindle in air; a pass that cut nothing, on a face that was not where the plan put
it, sounds like the air and would otherwise pass for stable. The face still counts as moved
on, and a verdict can be set by hand.

As the widget runs the round, it records where every cut and every dwell is, and those
windows are used as they are: the cut from where the cutter meets the stock to where it
leaves it, without any time the machine was held, and the dwell before it as the air
reference. A pass the run never reached is "not run", and a pass whose spindle speed was found
at the edge of its ±5% search is flagged ("speed?" in the widget). A pass the run cut, but where nothing
could be analysed (its window silent or too short, as when the recording stopped first), is
"no cut heard", with no verdict; its face still counts as moved on. A run that ended before
its first cut is not analysed, and the round stays planned to run again. Facing the top
down, wherever in the round it comes, is in the recording but in no cut window, so it is never
analysed.

`results.json` in each round holds the verdicts and the numbers behind them. If your ears
disagree with a verdict, change it in the widget before planning the next
round.

### How well it works

`validation/purdue.py` scores the detector against the
[Purdue CNC cutting sound dataset](https://github.com/purduelamm/mt_cutting_dataset): 583 cuts
in steel and aluminium on a Hurco VM20i, each labelled by two operators.

| Operators say | Cuts | Detector agrees |
|---|---|---|
| chatter (both) | 169 | 149 (88%) |
| no chatter (both) | 254 | 233 (92%) |
| slight, or they disagree | 160 | not scored |

The 27 dB threshold was chosen on this dataset, and spindle-speed and noise-floor handling were
checked against three PM-30MV widget sessions in 1018 steel (13 rounds, 57 passes). Removing
the spindle harmonics matters: without that step the best achievable balanced accuracy on the
Purdue data falls to 75%.

Known limits:

- A chatter tone that lands within a few hertz of a spindle harmonic is hidden. Its sidebands
  usually still trip the detector, but the reported frequency is then off by the tooth-passing
  frequency. This is likeliest at low speed, where the harmonics are close together.
- Flood coolant and a noisy shop raise the noise floor. Record close to the spindle.
- Variable-pitch cutters break the assumption that tooth passing is one frequency.

## How the next round is chosen

The planner follows Karandikar et al., "Evaluation of automated stability testing in machining
through closed-loop control and Bayesian machine learning"
([MSSP 181, 2022](https://doi.org/10.1016/j.ymssp.2022.109531)). It keeps a probability of
stability over the speed and width grid, starting from "wider is less likely to be stable".
A stable cut raises the probability of everything narrower near that speed, and an unstable
cut lowers everything wider. The next cuts are the ones with the highest expected improvement
in removal rate, and the session has converged when no cut promises 5% more. In a session
that steps the depth the grid is speed and depth, and everything said here of wider and
narrower holds for deeper and shallower.

Three things differ from the paper, which tests one cut at a time under closed-loop control:

- A round is a batch with no feedback between cuts. Later cuts in a batch assume the earlier
  ones pass, so widths can ramp at one speed, but each must also be at least 50% likely to be
  stable on measured data alone. A cut that builds on no earlier cut in the batch must be at
  least 80% likely.
- The paper does not give its likelihood. Here each speed has a distribution over its
  stability limit, each verdict is assumed wrong 5% of the time, and evidence fades with
  distance in speed (1.5% of the speed, one standard deviation).
- Chatter frequency is used. A chatter tone at `f` Hz predicts stable pockets where tooth
  passing divides it, at `60 f / (flutes k)` rpm. Those speeds get partial credit for the
  width that was stable where the chatter was heard, so the planner tries them.

The stock runs out before the search does on small blocks. When it does, mount fresh stock,
reset the face to `Y0` and plan on the "fresh block, face at Y0" in the widget.

### Lanes: fresh faces without remounting

A long block is split into lanes along X. By default there are as many as leave every cut
at least 6 s long at the fastest test speed, where cuts are shortest (`min_cut_s` in
`[planner]`, the wizard's Shortest cut to listen to), so a short block keeps a single lane;
the wizard shows the number it works out. `lanes` in `[stock]` (Lanes along X, under
Advanced in the wizard's Stock step) sets it by hand. The session's first round settles it:
it stays fixed even if a later change to the speed limits would allow more. The lanes are
used one after another, starting at the end the cutter meets first (the left end in
conventional milling, the right in climb). A round takes its cuts in order (narrowest
first; with a manual spindle, each speed's together) into what is left of the lane it
starts on, until the next cut does not fit; then it goes on in the next lane, and the one
after that, within the same program. Each new lane is a fresh face at `Y0`, at the same
height above the vise jaws as the first, with the same work zero, and starts with its own
truing pass. Each pass cuts one lane: it starts a cutter diameter clear of the lane before,
which is already cut back, and stops just short of the next lane, still untouched, then
backs off the face (the last lane runs out past the end of the stock as usual). A lane is
never cut further back than the one before it, since the cutter crosses that one on its
way in, and each lane must be at least twice the cutter diameter long. For the same reason
a cut in a later lane goes no deeper than the lane before it was cut that far back from
Y0; one deeper than that needs fresh stock (or the top faced down). The number of lanes
cannot change once a round was cut.
The program says in a comment where it changes lane; the widget's round
and the report list the lanes a round cuts and the pass each starts at. The next round
starts where the last pass heard left the stock, in whichever lane that was.

### Facing the top down

Given how far the top of the stock (where Z0 is set) stands above anything the cutter must
not reach (`height_mm` in `[stock]`, the wizard's Height above the jaws), dechatter faces the
top down itself once every lane is used, or when a deeper depth comes after a shallower one.
The program takes the top down below the deepest cut made on it (plus 0.2 mm), in levels of
at most `face_depth_mm` (1 mm) and rows `face_width_mm` (a quarter of the cutter) apart,
from the front face to the back (`thickness_mm`, or the width used) and past both ends, at
the speed of the cut that comes next. Then the round goes on at the first lane, below the
new top, with the same zero: no remounting, no re-zeroing. That can happen at the start of
a round or partway, when the lanes run out with cuts still to make, but at most once a
round: facing is slow, so the round uses the stock left in its lanes first, faces once,
fills the lanes of the new top, and drops the cuts that still do not fit; the next round
plans afresh. It faces only while the next cut's bottom stays the clearance above that
height; past that the round keeps the cuts that fit, and the next one asks for fresh stock.
The run time covers the facing.

Facing is no test cut, and is never analysed as one. The widget records on through it
and leaves it out of the pass windows it recorded, while keeping a record of each facing row.
A run that gets to facing partway leaves the stock as the facing left it: the new top if it
finished, the old one if it never started, and if it stopped part-way, a top faced only in
part, which the next round faces down again from the old top before any cut.

A chip-load sweep is planned only where all its cuts fit, in what the lanes have left, on
the lanes after them or on a faced top: cut short by the stock, its heaviest chip loads
would go untried and the last one tried would read as a limit. A sweep whose every chip
load stayed stable says so: its limit lies above them.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
node --test tests/js/stepper.test.mjs   # the widget's stepper against a scripted controller
uv run validation/purdue.py ~/purdue-cache   # downloads about 370 MB once
```

`e2e/` runs real cncjs against a simulated Grbl inside WSL, for driving the widget without a
machine. See `e2e/README.md`.

Every change users can notice adds a line under `## [Unreleased]` in
[`CHANGELOG.md`](CHANGELOG.md), in the same pull request: what they will notice and, for a
breaking change, what to do about it.

To release, set the same version in `src/dechatter/__init__.py` (`__version__`, which
`pyproject.toml` reads) and `package.json`, rename `## [Unreleased]` in `CHANGELOG.md` to
`## [X.Y.Z] - YYYY-MM-DD` above a fresh empty Unreleased section (and update the links at
its end), merge to `main`, and push a tag `vX.Y.Z` on that commit. The `release` workflow
refuses a tag without that dated section, tests on Linux and Windows, builds the widget
once, publishes that build to npm, and creates the GitHub release with the section as its
notes. npm trusts the workflow itself, so no token
is stored; version 0.1.0 was published by hand, because that trust can only be registered
for a package that exists.

## Related work

[Harmonizer](https://taptest.co/harmonizer) and
[ChatterPro Mobile](https://www.malinc.com/products/chatterpro-mobile/) are commercial phone
apps that recommend a speed from the sound of a cut. The test method, constant chip load with
stepped speed and width, is described in CNCCookbook's
[chatter article](https://www.cnccookbook.com/chatter-in-machining-milling-lathe-vibration/).

MIT licensed. The npm package also carries Pyodide, CPython, NumPy and SciPy under their own
licences; see `THIRD_PARTY_NOTICES.md`.
