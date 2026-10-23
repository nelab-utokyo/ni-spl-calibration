"""Run from repository root: uv run python examples/python_api.py"""

from pathlib import Path

from custom_sound import make_sound

from ni_spl_calibration import (
    SimulatedHardware,
    apply_calibration,
    calibrate,
    from_generator,
    load_setup,
    measure_reference,
    save_stimulus,
)
from ni_spl_calibration.storage import write_json


def main():
    setup = load_setup("examples/setup.yaml")
    stimulus = from_generator(
        make_sound,
        sample_rate_hz=setup.daq.sample_rate_hz,
        duration_s=2.0,
        carrier_hz=10000.0,
        am_hz=8.0,
        seed=42,
    )
    reference = measure_reference(setup, hardware=SimulatedHardware(setup.daq, reference_mode=True))
    result = calibrate(
        stimulus,
        setup=setup,
        reference=reference,
        target_spl_db=70,
        hardware=SimulatedHardware(setup.daq),
    )
    output = Path("results/python-example")
    output.mkdir(parents=True, exist_ok=False)
    save_stimulus(stimulus, output / "stimulus.npz")
    write_json(output / "reference.json", reference)
    write_json(output / "result.json", result)
    voltage = apply_calibration(stimulus, result)
    print(f"SIMULATION: {result.measured_spl_db:.3f} dB SPL; {max(abs(voltage)):.6g} Vpeak")


if __name__ == "__main__":
    main()
