from __future__ import annotations

from collections import defaultdict

import pytest

from smaract_home_ioc.routine import AxisConfig, HomeState, RoutineConfig, SmaractHomer


class FakeClient:
    def __init__(self):
        self.values = defaultdict(lambda: 0)
        self.puts = []
        self.rbv_values = []
        self.limit_after_reads = None
        self._limit_reads = 0

    def get(self, pv, *, timeout=2.0):
        if pv.endswith(".LLS") and self.limit_after_reads is not None:
            self._limit_reads += 1
            if self._limit_reads >= self.limit_after_reads:
                self.values[pv] = 1
        if pv.endswith(".RBV") and self.rbv_values:
            return self.rbv_values.pop(0)
        return self.values[pv]

    def put(self, pv, value, *, wait=False, timeout=10.0):
        self.puts.append((pv, value, wait))
        self.values[pv] = value
        if pv.endswith(".HOMF"):
            prefix = pv.removesuffix(".HOMF")
            self.values[f"{prefix}.MOVN"] = 0
            self.values[f"{prefix}.DMOV"] = 1
        if pv.endswith(".VAL"):
            prefix = pv.removesuffix(".VAL")
            self.values[f"{prefix}.MOVN"] = 0
            self.values[f"{prefix}.DMOV"] = 1


def ready_client(prefix="MTR"):
    client = FakeClient()
    client.values[f"{prefix}.CNEN"] = 1
    client.values[f"{prefix}.LVIO"] = 0
    client.values[f"{prefix}.SEVR"] = 0
    client.values[f"{prefix}.DMOV"] = 1
    client.values[f"{prefix}.MOVN"] = 0
    client.values[f"{prefix}.HLS"] = 0
    client.values[f"{prefix}.LLS"] = 0
    return client


def test_home_axis_sequence_sets_reference_and_moves_to_zero():
    client = ready_client()
    client.rbv_values = [10.0, 9.0]
    client.limit_after_reads = 2
    axis = AxisConfig("x", "MTR", home_position=-50000.0, negative_target=-1000000.0)
    config = RoutineConfig(
        search_timeout=2.0,
        home_timeout=2.0,
        move_timeout=2.0,
        motion_start_timeout=0.0,
        poll_period=0.0,
        stall_seconds=0.0,
        stall_tolerance=0.001,
        settle_seconds=0.0,
    )
    states = []

    state = SmaractHomer(client, config).home_axis(
        axis,
        progress=lambda state, message: states.append(state),
    )

    assert state == HomeState.HOMED
    assert ("MTR.VAL", -1000000.0, False) in client.puts
    assert ("MTR.STOP", 1, False) in client.puts
    assert ("MTR.HOMF", 1, False) in client.puts
    assert ("MTR.SET", 1, True) in client.puts
    assert ("MTR.VAL", -50000.0, True) in client.puts
    assert ("MTR.SET", 0, True) in client.puts
    assert client.puts[-1] == ("MTR.VAL", 0.0, False)
    assert states[-1] == HomeState.HOMED


def test_refuses_motor_alarm():
    client = ready_client()
    client.values["MTR.SEVR"] = 2
    axis = AxisConfig("x", "MTR", home_position=-50000.0, negative_target=-1000000.0)

    with pytest.raises(RuntimeError, match="alarm severity"):
        SmaractHomer(client).home_axis(axis)

    assert ("MTR.STOP", 1, False) in client.puts
