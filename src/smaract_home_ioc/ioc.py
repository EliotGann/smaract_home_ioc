from __future__ import annotations

import asyncio
import logging
import os
import re
from threading import Event

from caproto.server import PVGroup, ioc_arg_parser, pvproperty, run

from .epics_client import EpicsPVClient
from .routine import AxisConfig, HomeState, RoutineConfig, SmaractHomer


DEFAULT_IOC_PREFIX = "XF:12IDC-ES:SMARACT_HOME:"
DEFAULT_X_PREFIX = "XF:12ID2C-ES{MCS:2-Ax:3}Mtr"
DEFAULT_WBSX_PREFIX = "XF:12ID2C-ES{MCS:2-Ax:1}Mtr"
DEFAULT_NETSETUP_FILE = "/epics/common/xf12id2-inst-ioc1-netsetup.cmd"
EPICS_ENV_SET_RE = re.compile(
    r'^\s*epicsEnvSet\(\s*"(?P<name>[^"]+)"\s*,\s*"(?P<value>[^"]*)"\s*\)'
)
LOG = logging.getLogger(__name__)


def load_epics_netsetup(path: str = DEFAULT_NETSETUP_FILE) -> None:
    try:
        with open(path, encoding="utf-8") as netsetup:
            lines = netsetup.readlines()
    except FileNotFoundError:
        return

    for line in lines:
        match = EPICS_ENV_SET_RE.match(line)
        if match is None:
            continue
        os.environ[match.group("name")] = match.group("value")


def log_epics_network_config(run_options: dict) -> None:
    names = [
        "EPICS_CA_AUTO_ADDR_LIST",
        "EPICS_CA_ADDR_LIST",
        "EPICS_CAS_AUTO_BEACON_ADDR_LIST",
        "EPICS_CAS_BEACON_ADDR_LIST",
        "EPICS_CAS_INTF_ADDR_LIST",
    ]
    for name in names:
        LOG.warning("%s=%s", name, os.environ.get(name, ""))
    LOG.warning("caproto server interfaces=%s", run_options.get("interfaces"))


class SmaractHomeIOC(PVGroup):
    home_x = pvproperty(name="HomeX", value=0, dtype=int)
    home_wbsx = pvproperty(name="HomeWBSX", value=0, dtype=int)
    abort = pvproperty(name="Abort", value=0, dtype=int)
    arm = pvproperty(name="Arm", value=0, dtype=int)

    busy = pvproperty(name="Busy", value=0, dtype=int, read_only=True)
    active_axis = pvproperty(name="ActiveAxis", value="", dtype=str, read_only=True)
    status = pvproperty(name="Status", value=HomeState.IDLE.value, dtype=str, read_only=True)
    message = pvproperty(name="Message", value="idle", dtype=str, read_only=True)
    last_error = pvproperty(name="LastError", value="", dtype=str, read_only=True)

    x_motor_prefix = pvproperty(name="X:MotorPrefix", value=DEFAULT_X_PREFIX, dtype=str)
    wbsx_motor_prefix = pvproperty(name="WBSX:MotorPrefix", value=DEFAULT_WBSX_PREFIX, dtype=str)
    x_home_position = pvproperty(name="X:HomePosition", value=-50000.0)
    wbsx_home_position = pvproperty(name="WBSX:HomePosition", value=0.0)
    x_negative_target = pvproperty(name="X:NegativeTarget", value=-1000000.0)
    wbsx_negative_target = pvproperty(name="WBSX:NegativeTarget", value=-1000000.0)

    search_timeout = pvproperty(name="SearchTimeout", value=300.0)
    home_timeout = pvproperty(name="HomeTimeout", value=120.0)
    move_timeout = pvproperty(name="MoveTimeout", value=120.0)
    motion_start_timeout = pvproperty(name="MotionStartTimeout", value=5.0)
    poll_period = pvproperty(name="PollPeriod", value=0.25)
    stall_seconds = pvproperty(name="StallSeconds", value=5.0)
    stall_tolerance = pvproperty(name="StallTolerance", value=0.002)
    settle_seconds = pvproperty(name="SettleSeconds", value=1.0)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._task: asyncio.Task | None = None
        self._abort_event = Event()
        self._client = EpicsPVClient()

    @home_x.putter
    async def home_x(self, instance, value):
        if int(value) != 0:
            await self._start_home("x")
        return 0

    @home_wbsx.putter
    async def home_wbsx(self, instance, value):
        if int(value) != 0:
            await self._start_home("wbsx")
        return 0

    @abort.putter
    async def abort(self, instance, value):
        if int(value) != 0:
            self._abort_event.set()
            await self._write_message(HomeState.ABORTED, "abort requested")
        return 0

    async def _start_home(self, axis_name: str) -> None:
        if self._task is not None and not self._task.done():
            await self._write_error(f"cannot start {axis_name}: another homing routine is active")
            return
        if int(self.arm.value) != 1:
            await self._write_error(f"cannot start {axis_name}: set Arm=1 first")
            return

        self._abort_event.clear()
        await self.last_error.write("")
        self._task = asyncio.create_task(self._run_home(axis_name))

    def _axis_config(self, axis_name: str) -> AxisConfig:
        if axis_name == "x":
            return AxisConfig(
                name="x",
                motor_prefix=str(self.x_motor_prefix.value),
                home_position=float(self.x_home_position.value),
                negative_target=float(self.x_negative_target.value),
            )
        if axis_name == "wbsx":
            return AxisConfig(
                name="wbsx",
                motor_prefix=str(self.wbsx_motor_prefix.value),
                home_position=float(self.wbsx_home_position.value),
                negative_target=float(self.wbsx_negative_target.value),
            )
        raise ValueError(axis_name)

    def _routine_config(self) -> RoutineConfig:
        return RoutineConfig(
            search_timeout=float(self.search_timeout.value),
            home_timeout=float(self.home_timeout.value),
            move_timeout=float(self.move_timeout.value),
            motion_start_timeout=float(self.motion_start_timeout.value),
            poll_period=float(self.poll_period.value),
            stall_seconds=float(self.stall_seconds.value),
            stall_tolerance=float(self.stall_tolerance.value),
            settle_seconds=float(self.settle_seconds.value),
        )

    async def _run_home(self, axis_name: str) -> None:
        loop = asyncio.get_running_loop()
        axis = self._axis_config(axis_name)
        config = self._routine_config()
        homer = SmaractHomer(self._client, config)

        await self.busy.write(1)
        await self.active_axis.write(axis_name)
        await self._write_message(HomeState.CHECKING, f"{axis_name}: starting")

        def progress(state: HomeState, message: str) -> None:
            asyncio.run_coroutine_threadsafe(self._write_message(state, message), loop)

        try:
            await asyncio.to_thread(
                homer.home_axis,
                axis,
                abort_event=self._abort_event,
                progress=progress,
            )
        except Exception as exc:
            await self._write_error(str(exc))
        finally:
            await self.busy.write(0)
            await self.active_axis.write("")
            await self.arm.write(0)

    async def _write_message(self, state: HomeState, message: str) -> None:
        await self.status.write(state.value)
        await self.message.write(message)

    async def _write_error(self, message: str) -> None:
        await self.status.write(HomeState.ERROR.value)
        await self.message.write(message)
        await self.last_error.write(message)


def parse_ioc_args():
    return ioc_arg_parser(
        default_prefix=os.environ.get("SMARACT_HOME_IOC_PREFIX", DEFAULT_IOC_PREFIX),
        desc="Repeatable SmarAct piezo homing IOC",
    )


def main() -> None:
    load_epics_netsetup()
    ioc_options, run_options = parse_ioc_args()
    run_options["interfaces"] = ["0.0.0.0"]
    log_epics_network_config(run_options)
    prefix = ioc_options["prefix"]
    ioc = SmaractHomeIOC(prefix=prefix)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
