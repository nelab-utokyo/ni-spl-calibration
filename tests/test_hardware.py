"""Exercise DAQ sequencing and reset behavior without a driver or physical DAQ."""

import sys
from types import SimpleNamespace as NS

import numpy as np
import pytest

from ni_spl_calibration import DaqSettings, NIDaqHardware


def fake_driver(monkeypatch, *, fail_read=False, coerce_rate=False):
    events = []
    settings = DaqSettings(sample_rate_hz=20000)

    class Timing:
        def cfg_samp_clk_timing(self, rate, **kwargs):
            self.samp_clk_rate = rate + (1 if coerce_rate else 0)
            events.append(("timing", kwargs.get("source", ""), kwargs["samps_per_chan"]))

    class Task:
        kind = None

        def __init__(self):
            self.timing = Timing()
            self.ai_channels = NS(add_ai_voltage_chan=self.add_ai)
            self.ao_channels = NS(add_ao_voltage_chan=self.add_ao)
            self.in_stream = self
            self.out_stream = self

        def add_ai(self, *args, **kwargs):
            self.kind = "ai"

        def add_ao(self, *args, **kwargs):
            self.kind = "ao"
            events.append(("ao_channel", args[0], kwargs))
            return NS()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            events.append(("close", self.kind))

        def write(self, value, **kwargs):
            events.append(("reset", value))

        def control(self, mode):
            events.append(("commit", self.kind))
            events.append(
                (
                    "stream_config",
                    self.kind,
                    getattr(self, "regen_mode", None),
                    getattr(self, "output_buf_size", None),
                )
            )

        def start(self):
            events.append(("start", self.kind))

        def stop(self):
            events.append(("stop", self.kind))

        def wait_until_done(self, **kwargs):
            events.append(("done", self.kind))

    class Reader:
        def __init__(self, stream):
            pass

        def read_many_sample(self, values, **kwargs):
            if fail_read:
                raise RuntimeError("injected read failure")
            values[:] = 0
            return len(values)

    class Writer:
        def __init__(self, stream, **kwargs):
            assert kwargs["auto_start"] is False

        def write_many_sample(self, values, **kwargs):
            events.append(("write", len(values)))
            events.append(("written_values", values.copy()))
            return len(values)

    device = NS(
        name="Dev1",
        ai_physical_chans=NS(channel_names=["Dev1/ai0"]),
        ao_physical_chans=NS(channel_names=["Dev1/ao0"]),
        ai_max_single_chan_rate=1000000,
        ao_max_rate=1000000,
        product_type="synthetic DAQ",
        dev_serial_num=123,
    )
    system = NS(
        devices=[device], driver_version=NS(major_version=1, minor_version=2, update_version=3)
    )
    modules = {
        "nidaqmx": NS(Task=Task, DaqError=RuntimeError),
        "nidaqmx.system": NS(System=NS(local=lambda: system)),
        "nidaqmx.constants": NS(
            AcquisitionType=NS(FINITE="finite", CONTINUOUS="continuous"),
            TaskMode=NS(TASK_COMMIT="commit"),
            AOIdleOutputBehavior=NS(ZERO_VOLTS="zero"),
            TerminalConfiguration=NS(DIFF="diff", RSE="rse", NRSE="nrse", PSEUDO_DIFF="pseudo"),
            RegenerationMode=NS(DONT_ALLOW_REGENERATION="no_regeneration"),
        ),
        "nidaqmx.stream_readers": NS(AnalogSingleChannelReader=Reader),
        "nidaqmx.stream_writers": NS(AnalogSingleChannelWriter=Writer),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return NIDaqHardware(settings), events


def cleanup_events(events):
    return [event for event in events if event[0] in {"stop", "close", "reset"}]


def test_stop_interrupts_a_long_waveform_before_its_end(monkeypatch):
    from threading import Event

    from ni_spl_calibration import MeasurementCancelled

    hardware, events = fake_driver(monkeypatch)
    stop = Event()
    wave = 0.1 * np.sin(2 * np.pi * 1000 * np.arange(200000) / 20000)
    wave[-1] = 0
    received = []

    def interrupt(chunk):
        received.append(chunk)
        stop.set()

    with pytest.raises(MeasurementCancelled):
        hardware.play_and_record(wave, cancel_requested=stop.is_set, on_chunk=interrupt)
    assert sum(map(len, received)) == 400  # 20 ms, rather than the complete 10 s file.
    assert not any(event[0] == "done" for event in events)
    assert cleanup_events(events)[-6:] == [
        ("stop", "ao"),
        ("stop", "ai"),
        ("close", "ao"),
        ("close", "ai"),
        ("reset", 0.0),
        ("close", "ao"),
    ]


def test_streaming_refill_volume_and_stop(monkeypatch):
    from threading import Event

    from ni_spl_calibration import Stimulus
    from ni_spl_calibration.playback import VolumeControl, voltage_blocks

    hardware, events = fake_driver(monkeypatch)
    stop = Event()
    stimulus = Stimulus(0.8 * np.sin(2 * np.pi * 1000 * np.arange(4000) / 20000), 20000)
    volume = VolumeControl(0.1)
    blocks = voltage_blocks(stimulus, max_peak_v=0.5, block_size=1000, volume=volume, loop=True)
    stream = hardware.duplex(blocks, 1000, cancel_requested=stop.is_set)
    frames = [next(stream)]
    volume.set(0.8)
    frames.extend(next(stream) for _ in range(3))
    stop.set()
    assert list(stream) == []
    assert frames[0]["volume_fraction"] == 0.1
    assert frames[-1]["volume_fraction"] == 0.8
    written = [event[1] for event in events if event[0] == "written_values"]
    assert max(float(np.max(np.abs(values))) for values in written) <= 0.5
    assert any(np.max(np.abs(values)) > 0.3 for values in written)
    channels = [event for event in events if event[0] == "ao_channel"]
    assert all(event[1] == "Dev1/ao0" for event in channels)
    configuration = next(event for event in events if event[:2] == ("stream_config", "ao"))
    assert configuration[2:] == ("no_regeneration", 4000)
    assert events.index(("start", "ai")) < events.index(("start", "ao"))
    assert sum(event[0] == "write" for event in events[: events.index(("start", "ao"))]) == 2
    assert cleanup_events(events)[-6:] == [
        ("stop", "ao"),
        ("stop", "ai"),
        ("close", "ao"),
        ("close", "ai"),
        ("reset", 0.0),
        ("close", "ao"),
    ]


def test_shared_clock_and_ai_arming_before_output(monkeypatch):
    hardware, events = fake_driver(monkeypatch)
    assert hardware.play_and_record(np.zeros(100)).shape == (100,)
    assert ("timing", "/Dev1/ao/SampleClock", 100) in events
    assert events.index(("start", "ai")) < events.index(("start", "ao"))
    assert events.index(("write", 100)) < events.index(("start", "ai"))
    assert events[-2] == ("reset", 0.0)


def test_read_failure_closes_both_tasks_before_reset(monkeypatch):
    hardware, events = fake_driver(monkeypatch, fail_read=True)
    with pytest.raises(RuntimeError, match="injected"):
        hardware.play_and_record(np.zeros(100))
    cleanup = [event for event in events if event[0] in {"stop", "close", "reset"}]
    assert cleanup[-6:] == [
        ("stop", "ao"),
        ("stop", "ai"),
        ("close", "ao"),
        ("close", "ai"),
        ("reset", 0.0),
        ("close", "ao"),
    ]


def test_coerced_sample_rate_is_rejected_before_start(monkeypatch):
    hardware, events = fake_driver(monkeypatch, coerce_rate=True)
    with pytest.raises(RuntimeError, match="coerced"):
        hardware.play_and_record(np.zeros(100))
    assert not any(event[0] == "start" for event in events)
    assert events[-2] == ("reset", 0.0)


def test_output_limit_is_checked_before_creating_tasks(monkeypatch):
    hardware, events = fake_driver(monkeypatch)
    with pytest.raises(ValueError, match="exceeds"):
        hardware.play_and_record(np.array([0.6, 0.0]))
    assert events == []


def test_continuous_monitor_reuses_one_ai_task_and_closes_before_reset(monkeypatch):
    hardware, events = fake_driver(monkeypatch)
    stream = hardware.monitor(100, cancel_requested=lambda: False)
    first = next(stream)
    second = next(stream)
    assert first.shape == second.shape == (100,)
    assert first is not second
    assert events.count(("start", "ai")) == 1
    assert not any(event == ("start", "ao") for event in events)
    stream.close()
    cleanup = [event for event in events if event[0] in {"stop", "close", "reset"}]
    assert cleanup[-4:] == [("stop", "ai"), ("close", "ai"), ("reset", 0.0), ("close", "ao")]


def test_monitor_failure_or_coerced_rate_resets_output(monkeypatch):
    for options in ({"fail_read": True}, {"coerce_rate": True}):
        hardware, events = fake_driver(monkeypatch, **options)
        stream = hardware.monitor(100, cancel_requested=lambda: False)
        with pytest.raises(RuntimeError):
            next(stream)
        cleanup = [event for event in events if event[0] in {"close", "reset"}]
        assert cleanup[-3:] == [("close", "ai"), ("reset", 0.0), ("close", "ao")]
