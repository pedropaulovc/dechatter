A real 0.2.0 session (PM-30MV, ½" 2-flute HSS, manual spindle, 6061, 5.08 mm deep), cut
down to what the analysis tests need:

- `level.npy`: each round's recording as `audio.envelope_db` gives it (dB per 25 ms hop,
  float16), not the 57 and 40 MB recordings themselves.
- `plan.json`: the round's plan.
- `results-0.2.0.json`: what dechatter 0.2.0 made of the recording: cuts matched to the wrong
  passes (issue #32).
