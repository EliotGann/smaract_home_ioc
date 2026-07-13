from __future__ import annotations

import os
from unittest.mock import patch

from smaract_home_ioc import ioc as ioc_module
from smaract_home_ioc.ioc import load_epics_netsetup


def test_load_epics_netsetup_reads_epics_env_set_lines(tmp_path, monkeypatch):
    netsetup = tmp_path / "netsetup.cmd"
    netsetup.write_text(
        '\n'.join(
            [
                "# Ansible managed",
                'epicsEnvSet("EPICS_CA_AUTO_ADDR_LIST",          "NO")',
                'epicsEnvSet("EPICS_CA_ADDR_LIST",               "10.67.91.255")',
                'epicsEnvSet("EPICS_CAS_BEACON_ADDR_LIST",\t"10.67.91.255")',
                'epicsEnvSet("EPICS_CAS_INTF_ADDR_LIST",         "10.67.91.169")',
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
    assert os.environ["EPICS_CA_ADDR_LIST"] == "10.67.91.255"
    assert os.environ["EPICS_CAS_BEACON_ADDR_LIST"] == "10.67.91.255"
    assert os.environ["EPICS_CAS_INTF_ADDR_LIST"] == "10.67.91.169"


def test_load_epics_netsetup_overwrites_existing_environment(tmp_path, monkeypatch):
    netsetup = tmp_path / "netsetup.cmd"
    netsetup.write_text(
        'epicsEnvSet("EPICS_CA_ADDR_LIST", "10.67.91.255")',
        encoding="utf-8",
    )
    monkeypatch.setenv("EPICS_CA_ADDR_LIST", "192.0.2.255")

    load_epics_netsetup(str(netsetup))

    assert os.environ["EPICS_CA_ADDR_LIST"] == "10.67.91.255"


def test_load_epics_netsetup_ignores_missing_file(tmp_path):
    load_epics_netsetup(str(tmp_path / "missing.cmd"))


def test_main_loads_netsetup_before_parsing_ioc_args():
    calls = []

    def parse_ioc_args():
        calls.append("parse")
        return {"prefix": "TEST:"}, {"interfaces": ["0.0.0.0"]}

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
    assert run.call_args.kwargs["interfaces"] == ["0.0.0.0"]
