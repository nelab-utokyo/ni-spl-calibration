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
            return NS()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            events.append(("close", self.kind))

        def write(self, value, **kwargs):
            events.append(("reset", value))

        def control(self, mode):
            events.append(("commit", self.kind))

        def start(self):
            events.append(("start", self.kind))

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
            AcquisitionType=NS(FINITE="finite"),
            TaskMode=NS(TASK_COMMIT="commit"),
            AOIdleOutputBehavior=NS(ZERO_VOLTS="zero"),
            TerminalConfiguration=NS(DIFF="diff", RSE="rse", NRSE="nrse", PSEUDO_DIFF="pseudo"),
        ),
        "nidaqmx.stream_readers": NS(AnalogSingleChannelReader=Reader),
        "nidaqmx.stream_writers": NS(AnalogSingleChannelWriter=Writer),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return NIDaqHardware(settings), events


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
    assert events[-4:] == [("close", "ao"), ("close", "ai"), ("reset", 0.0), ("close", "ao")]


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
