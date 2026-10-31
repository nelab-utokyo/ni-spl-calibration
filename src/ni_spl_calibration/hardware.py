from __future__ import annotations

import importlib.metadata
import math
import platform
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict
from typing import Protocol

import numpy as np

from .config import DaqSettings
from .errors import MeasurementCancelled


class Hardware(Protocol):
    backend: str
    settings: DaqSettings

    def describe(self) -> dict: ...
    def reset_output(self) -> None: ...
    def acquire(self, sample_count: int, *, cancel_requested=None, on_chunk=None) -> np.ndarray: ...
    def play_and_record(
        self, output_v: np.ndarray, *, cancel_requested=None, on_chunk=None
    ) -> np.ndarray: ...
    def monitor(self, block_size: int, *, cancel_requested): ...
    def duplex(self, blocks, block_size: int, *, cancel_requested, total_samples=None): ...


def check_stop(cancel_requested):
    if cancel_requested is not None and cancel_requested():
        raise MeasurementCancelled("Measurement cancelled")


@contextmanager
def stop_tasks(*tasks):
    """Stop AO before AI, then let task contexts close before the 0 V reset."""
    primary = None
    try:
        yield
    except BaseException as error:
        primary = error
        raise
    finally:
        for task in tasks:
            try:
                task.stop()
            except Exception as error:
                if primary is None or isinstance(primary, GeneratorExit):
                    raise
                primary.add_note(f"DAQ task stop also failed: {error}")


@contextmanager
def output_guard(hardware: Hardware):
    hardware.reset_output()
    try:
        yield
    except BaseException as error:
        try:
            hardware.reset_output()
        except Exception as reset_error:
            error.add_note(f"AO reset also failed: {reset_error}. Verify the output physically.")
        raise
    else:
        hardware.reset_output()  # A failed reset must never be reported as success.


def runtime_versions() -> dict:
    versions = {"python": platform.python_version(), "platform": platform.system()}
    for package in (
        "ni-spl-calibration",
        "numpy",
        "scipy",
        "soundfile",
        "nidaqmx",
        "PySide6-Essentials",
    ):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    return versions


class NIDaqHardware:
    """One device with hardware-timed AI/AO and internal AO SampleClock routing."""

    backend = "ni-daqmx"

    def __init__(self, settings: DaqSettings):
        try:
            import nidaqmx
            from nidaqmx.system import System
        except ImportError as error:
            raise RuntimeError("Install the 'daq' extra and the NI-DAQmx driver") from error
        self.settings = settings
        self._daq = nidaqmx
        system = System.local()
        name = settings.input_channel.split("/")[0]
        devices = {device.name: device for device in system.devices}
        if name not in devices:
            raise RuntimeError(f"NI DAQ device is unavailable: {name}")
        device = devices[name]
        if settings.input_channel not in device.ai_physical_chans.channel_names:
            raise RuntimeError("Configured AI channel is unavailable")
        if settings.output_channel not in device.ao_physical_chans.channel_names:
            raise RuntimeError("Configured AO channel is unavailable")
        if (
            settings.sample_rate_hz > device.ai_max_single_chan_rate
            or settings.sample_rate_hz > device.ao_max_rate
        ):
            raise RuntimeError("Sample rate exceeds device AI/AO capability")
        driver = system.driver_version
        self._description = {
            "backend": self.backend,
            "device_name": name,
            "product_type": device.product_type,
            "serial_number": device.dev_serial_num,
            "driver_version": f"{driver.major_version}.{driver.minor_version}.{driver.update_version}",
            "runtime": runtime_versions(),
        }

    def describe(self):
        return dict(self._description)

    def reset_output(self):
        with self._daq.Task() as task:
            task.ao_channels.add_ao_voltage_chan(self.settings.output_channel)
            task.write(0.0, auto_start=True)

    def _input(self, task):
        from nidaqmx.constants import TerminalConfiguration

        names = {
            "differential": "DIFF",
            "rse": "RSE",
            "nrse": "NRSE",
            "pseudo_differential": "PSEUDO_DIFF",
        }
        task.ai_channels.add_ai_voltage_chan(
            self.settings.input_channel,
            terminal_config=getattr(
                TerminalConfiguration, names[self.settings.terminal_configuration]
            ),
            min_val=self.settings.input_min_v,
            max_val=self.settings.input_max_v,
        )

    def _check_rate(self, task):
        if not math.isclose(task.timing.samp_clk_rate, self.settings.sample_rate_hz, rel_tol=1e-9):
            raise RuntimeError("DAQ coerced the sample rate; choose an exactly supported rate")

    def _output(self, task):
        from nidaqmx.constants import AOIdleOutputBehavior

        channel = task.ao_channels.add_ao_voltage_chan(
            self.settings.output_channel,
            min_val=-self.settings.max_peak_v,
            max_val=self.settings.max_peak_v,
        )
        try:
            channel.ao_idle_output_behavior = AOIdleOutputBehavior.ZERO_VOLTS
        except self._daq.DaqError:
            pass  # Some devices do not support idle behavior; explicit reset remains mandatory.

    def _read_samples(self, reader, sample_count, *, cancel_requested=None, on_chunk=None):
        values = np.empty(sample_count)
        block_size = max(1, round(self.settings.sample_rate_hz * 0.02))
        for first in range(0, sample_count, block_size):
            check_stop(cancel_requested)
            part = values[first : first + block_size]
            count = reader.read_many_sample(
                part,
                number_of_samples_per_channel=len(part),
                timeout=max(0.25, len(part) / self.settings.sample_rate_hz + 0.1),
            )
            if count != len(part):
                raise RuntimeError("Incomplete AI read")
            if on_chunk is not None:
                on_chunk(part.copy())
            check_stop(cancel_requested)
        return values

    def _pair_clock(self, ai, ao, mode, sample_count, *, buffer_size=None):
        from nidaqmx.constants import TaskMode

        self._input(ai)
        self._output(ao)
        ao.timing.cfg_samp_clk_timing(
            self.settings.sample_rate_hz,
            sample_mode=mode,
            samps_per_chan=sample_count,
        )
        device = self.settings.output_channel.split("/")[0]
        ai.timing.cfg_samp_clk_timing(
            self.settings.sample_rate_hz,
            source=f"/{device}/ao/SampleClock",
            sample_mode=mode,
            samps_per_chan=sample_count,
        )
        if buffer_size is not None:
            from nidaqmx.constants import RegenerationMode

            ao.out_stream.regen_mode = RegenerationMode.DONT_ALLOW_REGENERATION
            ao.out_stream.output_buf_size = buffer_size
        ao.control(TaskMode.TASK_COMMIT)
        self._check_rate(ao)
        ai.control(TaskMode.TASK_COMMIT)
        self._check_rate(ai)

    def acquire(self, sample_count, *, cancel_requested=None, on_chunk=None):
        from nidaqmx.constants import AcquisitionType, TaskMode
        from nidaqmx.stream_readers import AnalogSingleChannelReader

        with output_guard(self):
            with self._daq.Task() as task:
                self._input(task)
                task.timing.cfg_samp_clk_timing(
                    self.settings.sample_rate_hz,
                    sample_mode=AcquisitionType.FINITE,
                    samps_per_chan=sample_count,
                )
                task.control(TaskMode.TASK_COMMIT)
                self._check_rate(task)
                with stop_tasks(task):
                    check_stop(cancel_requested)
                    task.start()
                    return self._read_samples(
                        AnalogSingleChannelReader(task.in_stream),
                        sample_count,
                        cancel_requested=cancel_requested,
                        on_chunk=on_chunk,
                    )

    def play_and_record(self, output_v, *, cancel_requested=None, on_chunk=None):
        from nidaqmx.constants import AcquisitionType
        from nidaqmx.stream_readers import AnalogSingleChannelReader
        from nidaqmx.stream_writers import AnalogSingleChannelWriter

        output_v = np.asarray(output_v, dtype=float)
        if output_v.ndim != 1 or len(output_v) < 2 or not np.all(np.isfinite(output_v)):
            raise ValueError("Provide a finite one-dimensional voltage waveform")
        if np.max(np.abs(output_v)) > self.settings.max_peak_v * (1 + 1e-12):
            raise ValueError("Output exceeds max_peak_v")
        if output_v[-1] != 0:
            raise ValueError("Output must end at 0 V")
        count = len(output_v)
        timeout = count / self.settings.sample_rate_hz + 5
        with output_guard(self):
            with self._daq.Task() as ai, self._daq.Task() as ao:
                self._pair_clock(ai, ao, AcquisitionType.FINITE, count)
                writer = AnalogSingleChannelWriter(ao.out_stream, auto_start=False)
                with stop_tasks(ao, ai):
                    check_stop(cancel_requested)
                    written = writer.write_many_sample(output_v, timeout=timeout)
                    if written != count:
                        raise RuntimeError("Incomplete AO write")
                    check_stop(cancel_requested)
                    ai.start()
                    ao.start()
                    values = self._read_samples(
                        AnalogSingleChannelReader(ai.in_stream),
                        count,
                        cancel_requested=cancel_requested,
                        on_chunk=on_chunk,
                    )
                    check_stop(cancel_requested)
                    ao.wait_until_done(timeout=0.25)
                    return values

    def duplex(self, blocks, block_size, *, cancel_requested, total_samples=None):
        """Stream non-regenerating AO with AI driven by the same AO sample clock."""
        from nidaqmx.constants import AcquisitionType
        from nidaqmx.stream_readers import AnalogSingleChannelReader
        from nidaqmx.stream_writers import AnalogSingleChannelWriter

        with output_guard(self):
            with self._daq.Task() as ai, self._daq.Task() as ao:
                mode = (
                    AcquisitionType.CONTINUOUS if total_samples is None else AcquisitionType.FINITE
                )
                self._pair_clock(
                    ai, ao, mode, total_samples or block_size * 4, buffer_size=block_size * 4
                )
                pending = deque()
                source = iter(blocks)
                writer = AnalogSingleChannelWriter(ao.out_stream, auto_start=False)

                def enqueue():
                    block = next(source, None)
                    if block is None:
                        return False
                    output = np.asarray(block["output"], dtype=float)
                    if output.shape != (block_size,) or not np.all(np.isfinite(output)):
                        raise ValueError("Invalid playback block")
                    if np.max(np.abs(output)) > self.settings.max_peak_v * (1 + 1e-12):
                        raise ValueError("Output exceeds max_peak_v")
                    check_stop(cancel_requested)
                    written = writer.write_many_sample(output, timeout=0.25)
                    if written != block_size:
                        raise RuntimeError("Incomplete AO streaming write")
                    pending.append(block)
                    return True

                with stop_tasks(ao, ai):
                    try:
                        enqueue()
                        enqueue()  # About 100 ms of lead, so fader changes remain responsive.
                        if not pending:
                            return
                        check_stop(cancel_requested)
                        ai.start()
                        ao.start()
                        reader = AnalogSingleChannelReader(ai.in_stream)
                        while pending:
                            values = self._read_samples(
                                reader, block_size, cancel_requested=cancel_requested
                            )
                            played = pending.popleft()
                            enqueue()  # Refill before GUI work or capture-file compression.
                            yield {**played, "samples": values}
                        check_stop(cancel_requested)
                        ao.wait_until_done(timeout=0.25)
                    except MeasurementCancelled:
                        return

    def monitor(self, block_size, *, cancel_requested):
        """Read consecutive blocks from one continuous AI task; keep AO at 0 V."""
        from nidaqmx.constants import AcquisitionType, TaskMode
        from nidaqmx.stream_readers import AnalogSingleChannelReader

        with output_guard(self):
            with self._daq.Task() as task:
                self._input(task)
                task.timing.cfg_samp_clk_timing(
                    self.settings.sample_rate_hz,
                    sample_mode=AcquisitionType.CONTINUOUS,
                    samps_per_chan=max(block_size * 20, 1000),
                )
                task.control(TaskMode.TASK_COMMIT)
                self._check_rate(task)
                reader = AnalogSingleChannelReader(task.in_stream)
                with stop_tasks(task):
                    task.start()
                    try:
                        while not cancel_requested():
                            yield self._read_samples(
                                reader, block_size, cancel_requested=cancel_requested
                            )
                    except MeasurementCancelled:
                        return


class SimulatedHardware:
    """Synthetic linear speaker and microphone, for software checks only."""

    backend = "simulation"

    def __init__(
        self,
        settings: DaqSettings,
        *,
        reference_frequency_hz=1000.0,
        reference_level_db=94.0,
        reference_mode=False,
        sensitivity_v_per_pa=0.1,
        pressure_pa_per_output_v=1.0,
        noise_rms_v=1e-7,
        seed=1,
    ):
        self.settings = settings
        self.reference_frequency_hz = reference_frequency_hz
        self.reference_level_db = reference_level_db
        self.reference_mode = reference_mode
        self.sensitivity_v_per_pa = sensitivity_v_per_pa
        self.pressure_pa_per_output_v = pressure_pa_per_output_v
        self.noise_rms_v = noise_rms_v
        self.rng = np.random.default_rng(seed)
        self._runtime = runtime_versions()

    def describe(self):
        return {
            "backend": self.backend,
            "model": "linear synthetic speaker/microphone",
            "sensitivity_v_per_pa": self.sensitivity_v_per_pa,
            "pressure_pa_per_output_v": self.pressure_pa_per_output_v,
            "noise_rms_v": self.noise_rms_v,
            "daq": asdict(self.settings),
            "runtime": dict(self._runtime),
        }

    def reset_output(self):
        pass

    def _emit_chunks(self, values, cancel_requested, on_chunk):
        block_size = max(1, round(self.settings.sample_rate_hz * 0.02))
        for first in range(0, len(values), block_size):
            check_stop(cancel_requested)
            if on_chunk is not None:
                on_chunk(values[first : first + block_size].copy())
        check_stop(cancel_requested)
        return values

    def acquire(self, sample_count, *, cancel_requested=None, on_chunk=None):
        values = self.rng.normal(0, self.noise_rms_v, sample_count)
        if self.reference_mode:
            t = np.arange(sample_count) / self.settings.sample_rate_hz
            pressure = 20e-6 * 10 ** (self.reference_level_db / 20)
            values += (
                np.sqrt(2)
                * pressure
                * self.sensitivity_v_per_pa
                * np.sin(2 * np.pi * self.reference_frequency_hz * t)
            )
        return self._emit_chunks(values, cancel_requested, on_chunk)

    def play_and_record(self, output_v, *, cancel_requested=None, on_chunk=None):
        if np.max(np.abs(output_v)) > self.settings.max_peak_v * (1 + 1e-12):
            raise ValueError("Output exceeds max_peak_v")
        values = (
            output_v * self.pressure_pa_per_output_v * self.sensitivity_v_per_pa
            + self.acquire(len(output_v))
        )
        return self._emit_chunks(values, cancel_requested, on_chunk)

    def duplex(self, blocks, block_size, *, cancel_requested, total_samples=None):
        with output_guard(self):
            for block in blocks:
                if cancel_requested():
                    return
                deadline = time.monotonic() + block_size / self.settings.sample_rate_hz
                while time.monotonic() < deadline:
                    if cancel_requested():
                        return
                    time.sleep(min(0.005, max(0, deadline - time.monotonic())))
                yield {
                    **block,
                    "samples": self.play_and_record(block["output"]),
                }

    def monitor(self, block_size, *, cancel_requested):
        with output_guard(self):
            interval = block_size / self.settings.sample_rate_hz
            while not cancel_requested():
                time.sleep(interval)
                if not cancel_requested():
                    yield self.acquire(block_size)
