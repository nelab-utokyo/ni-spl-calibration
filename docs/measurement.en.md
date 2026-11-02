# Measurement specification

[日本語](measurement.md) | [English](measurement.en.md) | [README](../README.en.md)

## Signal chain and reference

```text
Acoustic calibrator → microphone → conditioner → DAQ AI  (reference)
Waveform → DAQ AO → amplifier → speaker → microphone → DAQ AI
```

Reference `sensitivity_v_per_pa` covers the complete microphone, conditioner, cable and DAQ input chain in V/Pa. Reference acquisition checks frequency, pure-tone content, temporal stability and input clipping.

The DAQ must support hardware-timed AI/AO at the requested rate and internal routing of AO SampleClock to AI on the same device. AI is armed first and acquires using the AO clock. Unsupported routes and sample-rate coercion cause an error. [NI clock configuration API](https://nidaqmx-python.readthedocs.io/en/stable/task.html)

## Interval, band and SPL

Calibration plays and analyzes `measurement.start_s` through `stop_s`; `stop_s: null` selects the source end. Background is acquired beforehand for the same duration. Playback acquisition includes the acoustic delay and one final 0 V sample.

`band_hz: null` includes all AC bins up to Nyquist. FFT powers in the band are integrated, background power is subtracted, and the reference sensitivity converts the result to signal SPL. No A/C weighting is applied. Background is assumed stable during measurement and uncorrelated with the stimulus.

- Including silence or ramps changes the mean level. For AM sounds, include enough modulation cycles.
- The calculation uses whole-interval energy, not a median of block RMS or arithmetic mean of dB readings. The rectangular window imposes spectral-leakage and frequency-resolution limits.
- The same voltage scale must produce readings within tolerance multiple times. Acquisitions below the SNR requirement are not accepted as SPL readings.

Input monitoring and test playback show total SPL without background subtraction. Spectrum levels are approximately third-octave RMS voltages in dBV relative to 1 V.

## Frequency correction

`frequency_correction_db: [[Hz, dB], ...]` supplies additive corrections relative to the reference frequency. A relative sensitivity of +1 dB requires a −1 dB correction. Values are interpolated linearly in dB; a curve that does not cover the analysis band is rejected. An empty list applies no frequency-response correction. A 1 kHz reference alone does not establish accuracy throughout the ultrasonic range.

## Validity and limits

Calibration sets the overall level of a fixed waveform; it does not flatten the speaker response. For noise regenerated on each trial, check the level distribution over other seeds or representative waveforms. Stereo output, multiple-DAQ synchronization and infinite-stream calibration are outside scope.

Reference records are tied to setup identity, DAQ configuration and physical/simulation mode. Physical gain or wiring changes cannot be detected automatically: update the equipment record and remeasure.

Output limits and bounded upward steps are enforced; AO reset to 0 V is attempted after acquisition and on errors. Software alone cannot guarantee reset after disconnection or OS termination.

Automated tests use synthetic signals and mock DAQ tasks to validate software, not physical measurement accuracy or driver behavior.
