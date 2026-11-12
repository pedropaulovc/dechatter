# dechatter

dechatter finds the fastest chatter-free cut for one tool on one milling machine. It writes a
G-code program of test cuts, listens while the machine runs it, and decides from the sound
which cuts chattered. From that it either plans a finer round of cuts or reports the best
stable speeds and widths.

It runs as a widget inside [cncjs](https://cnc.js.org), entirely in the browser and recording
with that computer's microphone, or from the command line with a recording made on a phone.
The G-code is
plain Grbl dialect (`G0`, `G1`, `G4`, `M3`, `M5`), written for FluidNC and usable on Grbl and
grblHAL.

## What it measures

Chatter depends on the machine, the holder, the cutter and how far the cutter sticks out.
Machine weight, spindle power and material cannot predict it. They only bound the search:
the speed range, the power cap and the starting chip load. The cuts are the measurement, and
a result applies to that one tool assembly. Change the stickout and you start a new session.

Each round holds chip load and axial depth constant and varies spindle speed and radial width,
one pass per combination, cutting along the front face of a block held in the vise. Feed
follows speed so the chip load stays put.

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
depth. Planning a deeper cut on such a block is refused: mount fresh stock for it.

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
   or with no machine connected. Its last step holds the planner's settings, which rarely
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
   chip load, the stock length, the dwell and the coolant), since the stability map belongs
   to the setup it was measured with: start a new session instead. What only bounds the
   search may still change (the machine's speed range and power, the cut's speed and width
   limits and shallower depths), as may the machine's name, the safe height, clearance,
   plunge feed, truing width, stock width and the planner's settings. Cuts outside narrowed
   bounds stay in their round but are not recommended, nor are cuts over a lowered power
   cap. On the map, a stable cut wider than a narrowed width limit counts as stable at the
   widest width searched; chatter that wide, and cuts at speeds outside the limits, are left
   out. A round
   planned but not yet run is discarded when the setup changes its program, so the next
   plan uses the new setup. `dechatter plan` refuses the same changes made by hand.
2. Plan a round. Round 1 is a coarse grid of speeds and widths.
3. Set the work zero at the left end of the stock, on its front face, at the top.
4. Load the program into cncjs and press play in cncjs. The widget never starts the machine.
5. Recording starts when the job starts and stops when it ends. The widget then shows each
   pass as stable or chatter, the stability map and the best cuts so far.
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

Everything runs in the browser tab. The same Python that the command line uses runs there
under [Pyodide](https://pyodide.org), which `dechatter widget` downloads once and copies into
the folder, so the shop PC needs no Python and no internet. The microphone is the browser's,
on whichever computer shows cncjs, opened with echo cancellation, noise suppression and
automatic gain switched off. The widget shows its level before any cutting and warns if the
browser kept any of that processing on.

Sessions are stored in that browser (IndexedDB), per cncjs address. Export saves one as a zip
holding `setup.toml` and each round's plan, program, `recording.wav`, results and the job
timeline cncjs reported, plus `report.html`. Import loads it in another browser, and unzipped it is a session
folder the command line reads. Keep the tab open while a round runs: the recording lives in
the page until the job ends.

### Manual spindle

If the spindle is started and set by hand, choose "me, by hand" on the wizard's first step
(`spindle = "manual"` under `[machine]` in a setup file). The program then never commands the
spindle. It stops with `M0` before each speed, with
the cutter clear of the work, and the widget (and cncjs) shows the speed to set. Set it and
press resume in cncjs. Cuts are grouped by speed, lightest first, so a round has one stop per
speed: five in round 1. The first stop is where you start the spindle, and you stop it yourself
after the program ends.

Set the speed within 5% of what is asked; the detector finds the actual speed inside that
range. The stability map is built on the requested speeds, so a dial that reads off by a
constant amount shifts the whole map by that amount.

## Use from the command line

Without cncjs, record on a phone and run the same session by hand:

```sh
dechatter plan al-10mm          # writes al-10mm/round-01/program.nc
```

Set the work zero at the left end of the stock, on its front face, at the top. Start one
continuous recording, run the program, stop recording after `M30`. Then:

```sh
dechatter analyze al-10mm recording.wav   # classifies each pass
dechatter plan al-10mm                    # next round, or "converged"
dechatter plan al-10mm --sweep            # once converged: chip-load sweep of the best cut
dechatter report al-10mm                  # stability map and best cuts so far
dechatter report al-10mm --html al-10mm.html --units in   # printable report
```

Repeat until `plan` reports convergence. WAV, FLAC, OGG and MP3 load directly. Phone formats
such as M4A need `ffmpeg` on the path.

Round 1 is a coarse grid of speeds and widths. Later rounds are chosen from what was measured.
Every program orders its passes from the narrowest cut to the widest, starts with a light
truing pass, and tells you where the front face will end up.

## Safety

The program looks for chatter on purpose, so later passes are heavier than earlier ones and
some of them will chatter. Feed-hold and abort when a cut gets violent. Analyze the recording
anyway: the passes that ran still count, the aborted one is marked as such, and the next
round starts by clearing the step it left on the face.

Every planned cut stays under a power cap (60% of rated spindle power by default) and inside
the material's surface-speed limit. The built-in cutting data (`dechatter materials`) is a
conservative starting point. Override `chip_load_mm` and the speed range when you know better.

## How a pass is classified

Forced vibration sits on harmonics of the spindle rotation. Chatter is a strong tone anywhere
else. For each pass dechatter estimates the true spindle speed from the tooth-passing
harmonics, removes every spindle harmonic from the spectrum, and takes the strongest peak
left over. The pass is unstable when that peak is at least 27 dB above the median of the
remaining spectrum and at least 10 dB above the same frequency in the dwell just before the
pass, when the spindle was turning in air. The second test rejects tones the machine makes
without cutting, such as drive whine.

Passes are found in the recording by level, then matched in order to the planned passes by
how long each cut should last. A quiet pass that goes undetected is reported as missing and
does not shift the others. If matching fails, mark the cuts yourself and pass
`--segments labels.txt` (start and end seconds per pass; an Audacity label export works).

`results.json` in each round holds the verdicts and the numbers behind them. If your ears
disagree with a verdict, change it in the widget or edit it there before planning the next
round.

### How well it works

`validation/purdue.py` scores the detector against the
[Purdue CNC cutting sound dataset](https://github.com/purduelamm/mt_cutting_dataset): 583 cuts
in steel and aluminium on a Hurco VM20i, each labelled by two operators.

| Operators say | Cuts | Detector agrees |
|---|---|---|
| chatter (both) | 169 | 142 (84%) |
| no chatter (both) | 254 | 233 (92%) |
| slight, or they disagree | 160 | not scored |

The 27 dB threshold was chosen on this same data, so these numbers are a fit and the detector
has not been scored on a second dataset. Most of those cuts last about a second; dechatter's
own passes are longer, which gives a finer spectrum. Removing the spindle harmonics matters:
without that step the best achievable balanced accuracy on this data falls from 88% to 75%.

It has not yet been run on a real machine end to end. The planner and the audio pipeline are
tested against a simulated machine with stability lobes (`tests/synth.py`), where a session
converges in about three rounds and 27 cuts to within 10% of the true best removal rate. The
widget was driven through that same session in Chrome against cncjs 1.11.5 and a simulated
Grbl (`e2e/`), with the simulated machine's sound played into the widget in place of the
microphone. Capture from a real webcam microphone was checked separately, without cutting.

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
in removal rate, and the session has converged when no cut promises 5% more.

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

The stock runs out before the search does on small blocks. `plan` then asks for fresh stock;
mount it, reset the face to `Y0` and run `dechatter plan --new-stock`.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
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
