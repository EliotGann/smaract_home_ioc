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
        self.motion_on_val = True
        self.motion_on_homf = True

    def get(self, pv, *, timeout=2.0):
        if pv.endswith(".MOVN") and self.values[pv] != 0:
            self.values[pv] = 0
            return 1
        if pv.endswith(".DMOV") and self.values[pv] == 0:
            self.values[pv] = 1
            return 0
        if pv.endswith(".LLS") and self.limit_after_reads is not None:
            self._limit_reads += 1
            if self._limit_reads >= self.limit_after_reads:
                self.values[pv] = 1
        if pv.endswith(".RBV") and self.rbv_values:
            return self.rbv_values.pop(0)
        return self.values[pv]

    def get_for_log(self, pv, *, timeout=2.0):
        return self.values[pv]

    def put(self, pv, value, *, wait=False, timeout=10.0):
        self.puts.append((pv, value, wait))
        self.values[pv] = value
        if pv.endswith(".HOMF") and self.motion_on_homf:
            prefix = pv.removesuffix(".HOMF")
            self.values[f"{prefix}.MOVN"] = 1
            self.values[f"{prefix}.DMOV"] = 0
        if pv.endswith(".VAL") and self.motion_on_val:
            prefix = pv.removesuffix(".VAL")
            self.values[f"{prefix}.MOVN"] = 1
            self.values[f"{prefix}.DMOV"] = 0
            self.values[f"{prefix}.RBV"] = value
        if pv.endswith(".STOP"):
            prefix = pv.removesuffix(".STOP")
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
    client.values["MTR.LLM"] = -2000000.0
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


def test_home_axis_temporarily_lowers_blocking_low_soft_limit():
    client = ready_client()
    client.values["MTR.LLM"] = -100.0
    client.values["MTR.HLM"] = 1000000.0
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
    messages = []

    state = SmaractHomer(client, config).home_axis(
        axis,
        progress=lambda state, message: messages.append(message),
    )

    assert state == HomeState.HOMED
    assert client.puts.index(("MTR.LLM", -1000001.0, True)) < client.puts.index(
        ("MTR.VAL", -1000000.0, False)
    )
    assert client.puts.index(("MTR.VAL", 0.0, False)) < client.puts.index(("MTR.LLM", -100.0, True))
    assert client.values["MTR.LLM"] == -100.0
    assert any("temporarily lowering low soft limit" in message for message in messages)
    assert any("restoring low soft limit" in message for message in messages)


def test_home_axis_does_not_touch_unset_soft_limits():
    client = ready_client()
    client.values["MTR.LLM"] = 0.0
    client.values["MTR.HLM"] = 0.0
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
    messages = []

    state = SmaractHomer(client, config).home_axis(
        axis,
        progress=lambda state, message: messages.append(message),
    )

    assert state == HomeState.HOMED
    assert ("MTR.LLM", -1000001.0, True) not in client.puts
    assert ("MTR.LLM", 0.0, True) not in client.puts
    assert any("soft limits are unset" in message for message in messages)


def test_home_axis_errors_if_negative_move_does_not_start():
    client = ready_client()
    client.values["MTR.LLM"] = -2000000.0
    client.rbv_values = [10.0, 10.0]
    client.motion_on_val = False
    axis = AxisConfig("x", "MTR", home_position=-50000.0, negative_target=-1000000.0)
    config = RoutineConfig(
        search_timeout=2.0,
        motion_start_timeout=0.0,
        poll_period=0.0,
        stall_seconds=0.0,
        settle_seconds=0.0,
    )

    with pytest.raises(RuntimeError, match="negative move did not start"):
        SmaractHomer(client, config).home_axis(axis)

    assert ("MTR.VAL", -1000000.0, False) in client.puts
    assert ("MTR.HOMF", 1, False) not in client.puts
    assert ("MTR.STOP", 1, False) in client.puts


def test_home_forward_does_not_complete_without_motion():
    client = ready_client()
    client.values["MTR.LLM"] = -2000000.0
    client.rbv_values = [10.0, 9.0]
    client.limit_after_reads = 2
    client.motion_on_homf = False
    axis = AxisConfig("x", "MTR", home_position=-50000.0, negative_target=-1000000.0)
    config = RoutineConfig(
        search_timeout=2.0,
        home_timeout=0.05,
        move_timeout=2.0,
        motion_start_timeout=0.0,
        poll_period=0.0,
        stall_seconds=0.0,
        stall_tolerance=0.001,
        settle_seconds=0.0,
    )

    with pytest.raises(RuntimeError, match="timed out in finding_home"):
        SmaractHomer(client, config).home_axis(axis)

    # The routine must not race past a not-yet-started home into set-position
    # or the final move-to-zero, which is what allowed a phantom move after the
    # IOC reported HOMED and disarmed.
    assert ("MTR.SET", 1, True) not in client.puts
    assert ("MTR.VAL", 0.0, False) not in client.puts
    assert ("MTR.STOP", 1, False) in client.puts


def test_set_current_position_exits_set_mode_when_value_write_fails():
    client = ready_client()
    client.values["MTR.LLM"] = -2000000.0
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

    def fail_on_home_position(pv, value, *, wait=False, timeout=10.0):
        client.puts.append((pv, value, wait))
        if pv == "MTR.VAL" and value == -50000.0:
            raise TimeoutError("failed to set position")
        client.values[pv] = value
        if pv.endswith(".HOMF"):
            client.values["MTR.MOVN"] = 1
            client.values["MTR.DMOV"] = 0

    client.put = fail_on_home_position

    with pytest.raises(TimeoutError, match="failed to set position"):
        SmaractHomer(client, config).home_axis(axis)

    assert ("MTR.SET", 0, True) in client.puts
    assert client.puts.index(("MTR.SET", 1, True)) < client.puts.index(("MTR.SET", 0, True))


def test_low_soft_limit_warning_reports_blocking_limit():
    client = ready_client()
    client.values["MTR.LLM"] = -100.0
    client.values["MTR.HLM"] = 1000000.0
    axis = AxisConfig("x", "MTR", home_position=-50000.0, negative_target=-1000000.0)

    warning = SmaractHomer(client).low_soft_limit_warning(axis)

    assert warning is not None
    assert "low soft limit .LLM=-100 is not below" in warning
    assert "temporarily lower .LLM to -1000001" in warning


def test_low_soft_limit_warning_allows_limit_less_than_target():
    client = ready_client()
    client.values["MTR.LLM"] = -2000000.0
    client.values["MTR.HLM"] = 1000000.0
    axis = AxisConfig("x", "MTR", home_position=-50000.0, negative_target=-1000000.0)

    assert SmaractHomer(client).low_soft_limit_warning(axis) is None


def test_low_soft_limit_warning_reports_limit_equal_to_target():
    client = ready_client()
    client.values["MTR.LLM"] = -1000000.0
    client.values["MTR.HLM"] = 1000000.0
    axis = AxisConfig("x", "MTR", home_position=-50000.0, negative_target=-1000000.0)

    warning = SmaractHomer(client).low_soft_limit_warning(axis)

    assert warning is not None
    assert "low soft limit .LLM=-1000000 is not below" in warning


def test_low_soft_limit_warning_ignores_unset_soft_limits():
    client = ready_client()
    client.values["MTR.LLM"] = 0.0
    client.values["MTR.HLM"] = 0.0
    axis = AxisConfig("x", "MTR", home_position=-50000.0, negative_target=-1000000.0)

    assert SmaractHomer(client).low_soft_limit_warning(axis) is None


def test_refuses_motor_alarm():
    client = ready_client()
    client.values["MTR.SEVR"] = 2
    axis = AxisConfig("x", "MTR", home_position=-50000.0, negative_target=-1000000.0)

    with pytest.raises(RuntimeError, match="alarm severity"):
        SmaractHomer(client).home_axis(axis)

    assert ("MTR.STOP", 1, False) in client.puts
