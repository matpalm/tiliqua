## Paula2 +/-2 Octave Bring-Up Plan (Verbose)

### Goal
Build a new standalone top-level core at src/top/paula2 that keeps the proven Paula transport/timing path, but replaces sample-capture playback with a synthetic wavetable/ramp source specifically to validate reliable DMA servicing and strict +/-2 octave transposition behavior. A key realism requirement is that each AUDxDAT word carries two consecutive sample bytes (sequential packing), never duplicated byte pairs. The objective is to remove the current practical limitation where slowdown works but strong speed-up does not, while targeting a stable, realistic +/-2 octave range around a 1x center.

### Why the Existing Path Hits a Speed-Up Ceiling
The current paula path combines several assumptions that are valid for 1x-ish playback but constrain upward transposition:
- Sample capture rate is intentionally tied to a line-rate model in src/top/paula/sample.py (comments around one DMA word request per channel per line).
- Paula request visibility and servicing are coupled to the strhor-synchronous model in src/top/paula/top.py and Minimig Paula/Agnus RTL.
- Channel sample packing currently duplicates an 8-bit sample into both bytes of AUDxDAT in src/top/paula/channel.py, which is fine for the current test path but does not increase unique source advance at higher rates.

Real Paula supports pitch up and down, but this integration currently bottlenecks around how data is supplied and scheduled, not around Paula period register capability alone.

### Design Intent for Paula2
Paula2 should separate concerns:
- Paula transport layer: keep stable and faithful enough for clocking/register/DMA handshake behavior.
- Source transposition layer: perform controlled +/-2 octave pitch changes via phase-accumulator stepping in the data source, not by forcing AUDPER to carry the entire range alone.

This gives a path to controlled ratios (about 1/4x to 4x source step equivalence) while preserving DMA stability and reducing starvation risk.

### Scope Included
- New top-level folder: src/top/paula2.
- Include PaulaAudioWrapper in paula2 (same wrapper behavior as paula).
- New minimal fake_agnus helper in paula2 that emits synthetic data words for AUD0DAT using sequential-byte word packing (two successive sample bytes per 16-bit word).
- New paula2 top.py with:
  - clock/reset/domain setup,
  - pmod audio plumbing,
  - Paula wrapper integration,
  - register programming FSM,
  - DMA request capture/service,
  - startup priming and fallback anti-silence behavior,
  - internal source-phase sweep for strict +/-2 octave exercise.
- Add pdm command alias in pyproject.toml for paula2.

### Scope Excluded (for this phase)
- Real sample recording path.
- MIDI-based pitch control.
- Multi-channel balancing (initially channel 0 only).
- Documentation pages and script bundles outside minimal command alias.

### Implementation Plan

#### Phase 1: Structure and File Setup
1. Create src/top/paula2 directory.
2. Copy src/top/paula/paula_audio_wrapper.py to src/top/paula2/paula_audio_wrapper.py unchanged.
3. Create src/top/paula2/fake_agnus.py tailored for synthetic wavetable/ramp feeding.
4. Create src/top/paula2/top.py from paula top.py template, stripped of sample/channel/midi capture dependencies.

#### Phase 2: Preserve Proven Transport Timing Shell
5. Keep the same clk7 divider and strhor pulse structure currently used in paula top.
6. Keep reset hold and register write bus pattern (reg_addr/reg_data/reg_write + write_hold pacing).
7. Keep Paula instance wiring style and outbound audio routing to DAC path.

#### Phase 3: Minimal Channel-0 Paula Register Bring-Up
8. Program channel 0 registers during startup sequence:
   - AUD0PER (base transport period),
   - AUD0VOL,
   - AUD0LEN.
9. Keep DMA enable mask focused on channel 0 for minimal deterministic testing.
10. Keep audpen cleared unless explicitly needed for interrupt experiments.

#### Phase 4: DMA Service Robustness
11. Keep pending-request scheduler structure similar to paula top.
12. Preserve startup AUD0DAT prime writes so Paula has data immediately after init.
13. Keep fallback startup kick behavior to avoid silent lockup if request observation misses early handshakes.
14. Prefer robust request capture strategy to avoid starvation from short request windows.

#### Phase 5: +/-2 Octave Data Source in fake_agnus
15. Implement a phase-accumulator source that produces a repeating ramp/wavetable.
16. Pack each AUD0DAT word as two successive 8-bit samples generated from current phase and next phase advance.
17. Sweep phase increment internally (slow LFO/saw) across a controlled ratio window equivalent to approximately 1/4x to 4x around a nominal center (strict +/-2 octaves).
18. Keep phase increment representation fixed-point to support sub-1.0 and greater-than-1.0 stepping cleanly.

#### Phase 6: PDM Build Wiring
19. Add a new script alias in pyproject.toml:
   - paula2 = "src/top/paula2/top.py"
20. Confirm no other central registry edits are required for basic command invocation.

#### Phase 7: Validation
21. Validate command wiring with pdm paula2 -h.
22. Run pdm paula2 build on a known hardware target and confirm synthesis flow starts cleanly.
23. Verify no missing RTL source errors from wrapper inclusion.
24. Hardware smoke test:
   - audible output appears reliably,
   - no startup silence,
   - clearly audible downward and upward pitch movement over sweep.

### Key Risks and Mitigations
- Risk: DAT starvation due to request-capture timing mismatch.
  - Mitigation: keep pending queue, prime writes, and fallback kick logic.
- Risk: relying on AUDPER alone for the full +/-2 octave span can still collapse at the high end.
  - Mitigation: keep AUDPER near stable transport baseline and move transposition to source phase stepping.
- Risk: future reintegration friction with sample path.
  - Mitigation: shape fake_agnus interfaces so later captured-sample provider can replace synthetic source with minimal scheduler changes.

### Expected Outcome
At the end of this phase, paula2 should provide:
- a stable, minimal DMA-fed Paula audio pipeline,
- deterministic synthetic source data with sequential-byte AUDxDAT packing (no duplicated-byte words),
- bidirectional strict +/-2 octave transposition behavior driven by source stepping,
- a clean basis for reconnecting real sampled playback later without reworking core DMA scheduler architecture.
