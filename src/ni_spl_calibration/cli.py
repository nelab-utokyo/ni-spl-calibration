from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path

import yaml

from .calibration import (
    calibrate,
    load_reference,
    measure_reference,
    measurement_bounds,
    validate_reference,
)
from .config import Setup, load_setup
from .hardware import NIDaqHardware, SimulatedHardware
from .stimulus import from_generator, load_stimulus, load_wav, save_stimulus
from .storage import write_json


def parser():
    root = argparse.ArgumentParser(description="Calibrate finite mono waveforms with an NI DAQ")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Write an unmeasured setup template")
    init.add_argument("output", type=Path)
    inspect = commands.add_parser("inspect", help="Inspect a waveform without accessing hardware")
    _source_arguments(inspect)
    inspect.add_argument(
        "--sample-rate-hz",
        type=int,
        default=200_000,
        help="Generator sample rate (WAV/NPZ keep their own rate)",
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


def _stimulus(args, sample_rate_hz):
    if args.parameters and not args.generator:
        raise ValueError("--parameters requires --generator")
    if args.wav:
        return load_wav(args.wav)
    if args.npz:
        return load_stimulus(args.npz)
    parameters = {}
    if args.parameters:
        parameters = yaml.safe_load(args.parameters.read_text(encoding="utf-8"))
        if not isinstance(parameters, dict):
            raise ValueError("Generator parameters must be a YAML mapping")
    module_name, separator, function_name = args.generator.rpartition(":")
    if not separator or not module_name or not function_name:
        raise ValueError("Use --generator module:function or file.py:function")
    if module_name.endswith(".py"):
        path = Path(module_name)
        spec = importlib.util.spec_from_file_location("_ni_spl_user_generator", path)
        if spec is None or spec.loader is None:
            raise ValueError("Cannot import generator file")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(module_name)
    function = getattr(module, function_name)
    if not callable(function):
        raise ValueError("Generator must be callable")
    return from_generator(function, sample_rate_hz=sample_rate_hz, **parameters)


def _hardware(args, setup, reference_mode=False):
    if args.simulate:
        return SimulatedHardware(
            setup.daq,
            reference_mode=reference_mode,
            reference_frequency_hz=setup.reference_frequency_hz,
            reference_level_db=setup.reference_level_db,
        )
    return NIDaqHardware(setup.daq)


def _confirm(args, setup):
    if args.simulate:
        print("SIMULATION: results do not represent a physical acoustic calibration.")
    print(f"Setup: {setup.setup_id}; {setup.chain_notes}")
    print(
        f"AI: {setup.daq.input_channel} ({setup.daq.terminal_configuration}); "
        f"AO: {setup.daq.output_channel}; limit: {setup.daq.max_peak_v:g} Vpeak"
    )
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
        print(
            json.dumps(_stimulus(args, args.sample_rate_hz).describe(), indent=2, allow_nan=False)
        )
        return
    setup = load_setup(args.setup)
    if args.output.exists() or args.output.is_symlink():
        raise FileExistsError(f"Output already exists: {args.output}")
    if args.command == "reference":
        _confirm(args, setup)
        reference = measure_reference(setup, hardware=_hardware(args, setup, reference_mode=True))
        write_json(args.output, reference)
        print(f"Saved reference: {args.output}")
        return
    reference = load_reference(args.reference)
    validate_reference(setup, reference)
    stimulus = _stimulus(args, setup.daq.sample_rate_hz)
    measurement_bounds(stimulus, setup)
    _confirm(args, setup)
    hardware = _hardware(args, setup)
    validate_reference(setup, reference, hardware)
    args.output.mkdir(parents=True, exist_ok=False)
    save_stimulus(stimulus, args.output / "stimulus.npz")
    write_json(args.output / "setup.json", asdict(setup))
    write_json(args.output / "reference.json", reference)
    try:
        result = calibrate(
            stimulus,
            setup=setup,
            reference=reference,
            target_spl_db=args.target_spl_db,
            hardware=hardware,
            on_iteration=_iteration,
        )
    except (Exception, KeyboardInterrupt) as error:
        write_json(
            args.output / "failure.json",
            {
                "schema_version": 1,
                "status": "failed",
                "error": str(error) or "Interrupted",
                "notes": getattr(error, "__notes__", []),
                "iterations": getattr(error, "iterations", []),
                "stimulus": stimulus.describe(),
                "hardware": hardware.describe(),
            },
        )
        raise
    write_json(args.output / "result.json", result)
    print(f"Saved {args.output / 'result.json'}; scale = {result.scale_v:.8g} V/digital unit")


def main(argv=None):
    args = parser().parse_args(argv)
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
