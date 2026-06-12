# paula2 minimig RTL snapshot

might be wrong but the channel.v says attached modes not supported
so hackily bring it in explicitly here ( ? )

- added ADKCON audio attach-bit handling in `paula_audio.v` (ADKCON[7:0], set/clear semantics).
- attach modulation ch-to-ch paths in `paula_audio.v`:
  - CH0 modulated by CH3
  - CH1 modulated by CH0
  - CH2 modulated by CH1
  - CH3 modulated by CH2
- modulation inputs in `paula_audio_channel.v`:
  - `attach_vol_en`
  - `attach_per_en`
  - `attach_sample`
- modulation behavior in `paula_audio_channel.v`:
  - volume can be sourced from attached channel sample (`attach_sample[6:0]`).
  - period can be sourced from attached channel sample (`{8'h00, attach_sample} + 1`).
