from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import yaml

from .calibration import load_reference, measurement_bounds, validate_reference
from .config import DEFAULT_SAMPLE_RATE_HZ, Setup, load_setup
from .workflow import (
    StimulusSource,
    calibrate_to_directory,
    make_hardware,
    measure_reference_to_file,
)


def parser():
    root = argparse.ArgumentParser(
        description="NI SPL Calibration. Launch the desktop GUI when no command is supplied."
    )
    commands = root.add_subparsers(dest="command")
    commands.add_parser("gui", help="Open the desktop application (default)")
    init = commands.add_parser("init", help="Write an unmeasured setup template")
    init.add_argument("output", type=Path)
    inspect = commands.add_parser("inspect", help="Inspect a waveform without accessing hardware")
    _source_arguments(inspect)
    rate = inspect.add_mutually_exclusive_group()
    rate.add_argument(
        "--setup",
        type=Path,
        help="Use the same DAQ/WAV/generator sample rate as calibration",
    )
    rate.add_argument(
        "--sample-rate-hz",
        type=int,
        help=f"Standalone WAV/generator target rate (generator default: {DEFAULT_SAMPLE_RATE_HZ})",
    )
    reference = commands.add_parser("reference", help="Measure the acoustic-calibrator reference")
    reference.add_argument("setup", type=Path)
    reference.add_argument("--output", type=Path, required=True, help="New reference JSON file")
    _hardware_arguments(reference)
    search = commands.add_parser("calibrate", help="Search an output scale for a waveform")
    search.add_argument("setup", type=Path)
    search.add_argument("--reference", type=Path, required=True)
    search.add_argument("--target-spl-db", type=float, required=True)
    search.add_argument("--output", type=Path, required=True, help="New result directory")
    _source_arguments(search)
    _hardware_arguments(search)
    return root


def _source_arguments(command):
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument("--wav", type=Path)
    source.add_argument("--npz", type=Path, help="Previously saved stimulus.npz")
    source.add_argument("--generator", help="module:function or path/to/file.py:function")
    command.add_argument("--parameters", type=Path, help="Generator keyword parameters in YAML")


def _hardware_arguments(command):
    command.add_argument(
        "--simulate",
        action="store_true",
        help="Use synthetic hardware; produces simulation records",
    )
    command.add_argument(
        "--yes", action="store_true", help="Skip the hardware-operation confirmation"
    )


def _stimulus(args, sample_rate_hz=None):
    kind = next(name for name in ("wav", "npz", "generator") if getattr(args, name))
    return StimulusSource(kind, str(getattr(args, kind)), args.parameters).prepare(sample_rate_hz)


def _hardware(args, setup, reference_mode=False):
    return make_hardware(setup, simulate=args.simulate, reference_mode=reference_mode)


def _confirm(args, setup, stimulus=None):
    if args.simulate:
        print("SIMULATION: results do not represent a physical acoustic calibration.")
    print(f"Setup: {setup.setup_id}; {setup.chain_notes}")
    print(
        f"AI: {setup.daq.input_channel} ({setup.daq.terminal_configuration}); "
        f"AO: {setup.daq.output_channel}; sample rate: {setup.daq.sample_rate_hz} Hz; "
        f"limit: {setup.daq.max_peak_v:g} Vpeak"
    )
    if stimulus is not None and "resampling" in stimulus.provenance:
        source_rate = stimulus.provenance["resampling"]["source_sample_rate_hz"]
        print(f"WAV resampled: {source_rate} -> {stimulus.sample_rate_hz} Hz before calibration.")
    if args.command == "reference":
        print(
            f"Fit the calibrator: {setup.reference_level_db:g} dB SPL, "
            f"{setup.reference_frequency_hz:g} Hz. AO stays at 0 V."
        )
    else:
        print("Remove the calibrator; place the microphone at the measurement position.")
    if (
        not args.yes
        and input("Ready for measurement (no animals/listeners exposed)? Type yes: ").strip()
        != "yes"
    ):
        raise ValueError("Measurement cancelled")


def _iteration(item):
    level = item["measured_spl_db"]
    label = "below SNR requirement" if level is None else f"{level:.3f} dB SPL"
    print(f"{item['iteration']:02d}: {item['output_peak_v']:.6g} Vpeak -> {label}")


def _run(args):
    if args.command == "init":
        setup = Setup(
            "my-setup",
            "Record microphone/calibrator serials, conditioner gain/filters, speaker, amplifier, cabling and geometry",
        )
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(yaml.safe_dump(asdict(setup), sort_keys=False, allow_unicode=True))
        print(
            f"Created {args.output}; edit the setup identity and hardware settings before measurement."
        )
        return
    if args.command == "inspect":
        sample_rate_hz = (
            load_setup(args.setup).daq.sample_rate_hz if args.setup else args.sample_rate_hz
        )
        print(json.dumps(_stimulus(args, sample_rate_hz).describe(), indent=2, allow_nan=False))
        return
    setup = load_setup(args.setup)
    if args.output.exists() or args.output.is_symlink():
        raise FileExistsError(f"Output already exists: {args.output}")
    if args.command == "reference":
        _confirm(args, setup)
        measure_reference_to_file(setup, args.output, simulate=args.simulate)
        print(f"Saved reference: {args.output}")
        return
    reference = load_reference(args.reference)
    validate_reference(setup, reference)
    stimulus = _stimulus(args, setup.daq.sample_rate_hz)
    measurement_bounds(stimulus, setup)
    _confirm(args, setup, stimulus)
    hardware = _hardware(args, setup)
    validate_reference(setup, reference, hardware)
    result = calibrate_to_directory(
        stimulus,
        setup=setup,
        reference=reference,
        target_spl_db=args.target_spl_db,
        output=args.output,
        hardware=hardware,
        on_iteration=_iteration,
    )
    print(f"Saved {args.output / 'result.json'}; scale = {result.scale_v:.8g} V/digital unit")


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command in (None, "gui"):
        from .gui import main as gui_main

        return gui_main()
    try:
        _run(args)
    except KeyboardInterrupt as error:
        print(
            "Interrupted; AO reset was attempted. Verify output before resuming.", file=sys.stderr
        )
        for note in getattr(error, "__notes__", []):
            print(note, file=sys.stderr)
        return 130
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        for note in getattr(error, "__notes__", []):
            print(note, file=sys.stderr)
        return 2
    return 0
