Real widget cuts from three PM-30MV sessions in 1018 steel (1/2 in 3-flute carbide in R8,
manual spindle), decimated from 48 kHz to 24 kHz (flat across the 150-8000 Hz analysis band)
and 8-bit mu-law encoded so the nine pass/dwell pairs take ~1.7 MB instead of 200 MB:

- `v5_r01_p07`, `v5_r02_p03` and `v5_r03_p03`: clean stable passes across speeds and widths up
  to 3.4 mm at 1.27 mm depth (`19.7-25.8 dB` prominence).
- `v6_r02_p02` and `v6_r03_p01`: heavy 6.6 mm passes (`2.2 mm` and `2.7 mm` wide) whose
  chatter wandered across Welch bins and raised the non-harmonic floor `+25-30 dB` over the
  air dwell, masking single-bin prominence before `PEAK_WANDER_HZ` and `MAX_FLOOR_RISE_DB`.
- `v6_r03_p03` and `v6_r04_p01`: violent/clipped chatter at `6.6 mm` (`+34 dB` floor rise) and
  `3.2 mm` depth whose dominant tones pulled tooth-only spindle estimation to `SPINDLE_EDGE`.
- `i3439_r04_p01` and `i3439_r05_p01`: chattering `7.1 mm` and `7.6 mm` wide cuts at `1.27 mm`
  depth whose frequency-drifting chatter tones spread across adjacent bins.
