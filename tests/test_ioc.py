from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

from smaract_home_ioc import ioc as ioc_module
from smaract_home_ioc.ioc import DEFAULT_X_PREFIX, SmaractHomeIOC, load_epics_netsetup
from smaract_home_ioc.routine import HOME_STATE_CODES, HomeState


@pytest.fixture(autouse=True)
def mock_epics_client():
    with patch.object(ioc_module, "EpicsPVClient"):
        yield


def test_load_epics_netsetup_reads_epics_env_set_lines(tmp_path, monkeypatch):
    netsetup = tmp_path / "netsetup.cmd"
    netsetup.write_text(
        '\n'.join(
            [
                "# Ansible managed",
                'epicsEnvSet("EPICS_CA_AUTO_ADDR_LIST",          "NO")',
                'epicsEnvSet("EPICS_CA_ADDR_LIST",               "configured-ca-target")',
                'epicsEnvSet("EPICS_CAS_BEACON_ADDR_LIST",\t"configured-beacon-target")',
                'epicsEnvSet("EPICS_CAS_INTF_ADDR_LIST",         "configured-interface")',
            ]
        ),
        encoding="utf-8",
    )
    for name in [
        "EPICS_CA_AUTO_ADDR_LIST",
        "EPICS_CA_ADDR_LIST",
        "EPICS_CAS_BEACON_ADDR_LIST",
        "EPICS_CAS_INTF_ADDR_LIST",
    ]:
        monkeypatch.delenv(name, raising=False)

    load_epics_netsetup(str(netsetup))

    assert os.environ["EPICS_CA_AUTO_ADDR_LIST"] == "NO"
    assert os.environ["EPICS_CA_ADDR_LIST"] == "configured-ca-target"
    assert os.environ["EPICS_CAS_BEACON_ADDR_LIST"] == "configured-beacon-target"
    assert os.environ["EPICS_CAS_INTF_ADDR_LIST"] == "configured-interface"


def test_load_epics_netsetup_preserves_existing_environment(tmp_path, monkeypatch):
    netsetup = tmp_path / "netsetup.cmd"
    netsetup.write_text(
        'epicsEnvSet("EPICS_CA_ADDR_LIST", "netsetup-target")',
        encoding="utf-8",
    )
    monkeypatch.setenv("EPICS_CA_ADDR_LIST", "environment-target")

    load_epics_netsetup(str(netsetup))

    assert os.environ["EPICS_CA_ADDR_LIST"] == "environment-target"


def test_load_epics_netsetup_ignores_missing_file(tmp_path):
    load_epics_netsetup(str(tmp_path / "missing.cmd"))


def test_home_state_codes_are_stable():
    assert HOME_STATE_CODES == {
        HomeState.IDLE: 0,
        HomeState.CHECKING: 1,
        HomeState.FINDING_NEGATIVE_LIMIT: 2,
        HomeState.FINDING_HOME: 3,
        HomeState.SETTING_POSITION: 4,
        HomeState.MOVING_TO_ZERO: 5,
        HomeState.HOMED: 6,
        HomeState.ABORTED: 7,
        HomeState.ERROR: 8,
    }


def test_arm_warns_when_low_soft_limit_blocks_negative_target():
    ioc = SmaractHomeIOC(prefix="TEST:")
    values = {
        f"{DEFAULT_X_PREFIX}.LLM": -100.0,
        f"{DEFAULT_X_PREFIX}.HLM": 1000000.0,
        f"{ioc.wbsx_motor_prefix.value}.LLM": -2000000.0,
        f"{ioc.wbsx_motor_prefix.value}.HLM": 1000000.0,
    }

    class Client:
        def get(self, pv, *, timeout=2.0):
            return values[pv]

        def put(self, pv, value, *, wait=False, timeout=10.0):
            values[pv] = value

    ioc._client = Client()

    asyncio.run(ioc.arm.write(1))

    assert ioc.arm.value == 1
    assert ioc.message.value == (
        "x: low soft limit .LLM=-100 is not below NegativeTarget=-1000000; "
        "homing will temporarily lower .LLM to -1000001"
    )


def test_arm_ignores_unset_soft_limits():
    ioc = SmaractHomeIOC(prefix="TEST:")
    values = {
        f"{DEFAULT_X_PREFIX}.LLM": 0.0,
        f"{DEFAULT_X_PREFIX}.HLM": 0.0,
        f"{ioc.wbsx_motor_prefix.value}.LLM": 0.0,
        f"{ioc.wbsx_motor_prefix.value}.HLM": 0.0,
    }

    class Client:
        def get(self, pv, *, timeout=2.0):
            return values[pv]

        def put(self, pv, value, *, wait=False, timeout=10.0):
            values[pv] = value

    ioc._client = Client()

    asyncio.run(ioc.arm.write(1))

    assert ioc.arm.value == 1
    assert ioc.message.value == "idle"


def test_main_loads_netsetup_before_parsing_and_preserves_interfaces():
    calls = []

    def parse_ioc_args():
        calls.append("parse")
        return {"prefix": "TEST:"}, {"interfaces": ["configured-interface"]}

    def load_epics_netsetup():
        calls.append("load")

    with (
        patch.object(ioc_module, "parse_ioc_args", parse_ioc_args),
        patch.object(ioc_module, "load_epics_netsetup", load_epics_netsetup),
        patch.object(ioc_module, "run") as run,
    ):
        ioc_module.main()

    assert calls == ["load", "parse"]
    assert run.call_args.args[0]
    assert run.call_args.kwargs["interfaces"] == ["configured-interface"]
