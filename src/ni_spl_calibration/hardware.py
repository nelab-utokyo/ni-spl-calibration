from __future__ import annotations

import importlib.metadata
import math
import platform
from contextlib import contextmanager
from dataclasses import asdict
from typing import Protocol

import numpy as np

from .config import DaqSettings


class Hardware(Protocol):
    backend: str
    settings: DaqSettings

    def describe(self) -> dict: ...
    def reset_output(self) -> None: ...
    def acquire(self, sample_count: int) -> np.ndarray: ...
    def play_and_record(self, output_v: np.ndarray) -> np.ndarray: ...


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
    for package in ("ni-spl-calibration", "numpy", "scipy", "soundfile", "nidaqmx"):
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

    def acquire(self, sample_count):
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
                values = np.empty(sample_count)
                task.start()
                count = AnalogSingleChannelReader(task.in_stream).read_many_sample(
                    values,
                    number_of_samples_per_channel=sample_count,
                    timeout=sample_count / self.settings.sample_rate_hz + 5,
                )
                if count != sample_count:
                    raise RuntimeError("Incomplete AI capture")
                return values

    def play_and_record(self, output_v):
        from nidaqmx.constants import AcquisitionType, AOIdleOutputBehavior, TaskMode
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
                self._input(ai)
                channel = ao.ao_channels.add_ao_voltage_chan(
                    self.settings.output_channel,
                    min_val=-self.settings.max_peak_v,
                    max_val=self.settings.max_peak_v,
                )
                try:
                    channel.ao_idle_output_behavior = AOIdleOutputBehavior.ZERO_VOLTS
                except self._daq.DaqError:
                    pass  # Some devices do not support idle behavior; explicit reset remains mandatory.
                ao.timing.cfg_samp_clk_timing(
                    self.settings.sample_rate_hz,
                    sample_mode=AcquisitionType.FINITE,
                    samps_per_chan=count,
                )
                # AI is armed first and receives exactly the AO sample-clock edges.
                device = self.settings.output_channel.split("/")[0]
                ai.timing.cfg_samp_clk_timing(
                    self.settings.sample_rate_hz,
                    source=f"/{device}/ao/SampleClock",
                    sample_mode=AcquisitionType.FINITE,
                    samps_per_chan=count,
                )
                ao.control(TaskMode.TASK_COMMIT)
                self._check_rate(ao)
                ai.control(TaskMode.TASK_COMMIT)
                writer = AnalogSingleChannelWriter(ao.out_stream, auto_start=False)
                written = writer.write_many_sample(output_v, timeout=timeout)
                if written != count:
                    raise RuntimeError("Incomplete AO write")
                values = np.empty(count)
                ai.start()
                ao.start()
                read = AnalogSingleChannelReader(ai.in_stream).read_many_sample(
                    values, number_of_samples_per_channel=count, timeout=timeout
                )
                if read != count:
                    raise RuntimeError("Incomplete AI read")
                ao.wait_until_done(timeout=timeout)
                return values


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

    def describe(self):
        return {
            "backend": self.backend,
            "model": "linear synthetic speaker/microphone",
            "sensitivity_v_per_pa": self.sensitivity_v_per_pa,
            "pressure_pa_per_output_v": self.pressure_pa_per_output_v,
            "noise_rms_v": self.noise_rms_v,
            "daq": asdict(self.settings),
            "runtime": runtime_versions(),
        }

    def reset_output(self):
        pass

    def acquire(self, sample_count):
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
        return values

    def play_and_record(self, output_v):
        if np.max(np.abs(output_v)) > self.settings.max_peak_v * (1 + 1e-12):
            raise ValueError("Output exceeds max_peak_v")
        return output_v * self.pressure_pa_per_output_v * self.sensitivity_v_per_pa + self.acquire(
            len(output_v)
        )
