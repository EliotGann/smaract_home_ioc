from __future__ import annotations

import asyncio
import logging
import os
import re
from threading import Event

from caproto.server import PVGroup, ioc_arg_parser, pvproperty, run

from .epics_client import EpicsPVClient
from .routine import HOME_STATE_CODES, AxisConfig, HomeState, RoutineConfig, SmaractHomer


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
        os.environ.setdefault(match.group("name"), match.group("value"))


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
    status_code = pvproperty(
        name="StatusCode",
        value=HOME_STATE_CODES[HomeState.IDLE],
        dtype=int,
        read_only=True,
    )
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
    post_home_monitor_seconds = pvproperty(name="PostHomeMonitorSeconds", value=20.0)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._task: asyncio.Task | None = None
        self._abort_event = Event()
        self._client = EpicsPVClient()

    @home_x.putter
    async def home_x(self, instance, value):
        LOG.warning(
            "HomeX put value=%r arm=%r busy=%r active_axis=%r",
            value,
            self.arm.value,
            self.busy.value,
            self.active_axis.value,
        )
        if int(value) != 0:
            await self._start_home("x")
        return 0

    @home_wbsx.putter
    async def home_wbsx(self, instance, value):
        LOG.warning(
            "HomeWBSX put value=%r arm=%r busy=%r active_axis=%r",
            value,
            self.arm.value,
            self.busy.value,
            self.active_axis.value,
        )
        if int(value) != 0:
            await self._start_home("wbsx")
        return 0

    @abort.putter
    async def abort(self, instance, value):
        LOG.warning(
            "Abort put value=%r arm=%r busy=%r active_axis=%r",
            value,
            self.arm.value,
            self.busy.value,
            self.active_axis.value,
        )
        if int(value) != 0:
            self._abort_event.set()
            await self._write_message(HomeState.ABORTED, "abort requested")
        return 0

    @arm.putter
    async def arm(self, instance, value):
        LOG.warning(
            "Arm put value=%r current_arm=%r busy=%r active_axis=%r",
            value,
            self.arm.value,
            self.busy.value,
            self.active_axis.value,
        )
        value = int(value)
        if value == 1:
            await self._warn_blocking_low_soft_limits()
        return value

    async def _start_home(self, axis_name: str) -> None:
        LOG.warning(
            "home requested for %s arm=%r busy=%r active_axis=%r task=%r task_done=%r",
            axis_name,
            self.arm.value,
            self.busy.value,
            self.active_axis.value,
            self._task,
            None if self._task is None else self._task.done(),
        )
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

    async def _warn_blocking_low_soft_limits(self) -> None:
        homer = SmaractHomer(self._client, self._routine_config())
        warnings = []
        for axis_name in ("x", "wbsx"):
            axis = self._axis_config(axis_name)
            try:
                warning = homer.low_soft_limit_warning(axis)
            except Exception as exc:
                LOG.warning("could not check %s low soft limit before arming: %s", axis_name, exc)
                continue
            if warning is not None:
                warnings.append(warning)

        if warnings:
            await self._write_message(HomeState.CHECKING, "; ".join(warnings))

    async def _run_home(self, axis_name: str) -> None:
        loop = asyncio.get_running_loop()
        axis = self._axis_config(axis_name)
        config = self._routine_config()
        homer = SmaractHomer(self._client, config)

        LOG.warning(
            "home run configured axis=%s motor_prefix=%s home_position=%s negative_target=%s "
            "search_timeout=%s home_timeout=%s move_timeout=%s motion_start_timeout=%s "
            "poll_period=%s stall_seconds=%s stall_tolerance=%s settle_seconds=%s",
            axis.name,
            axis.motor_prefix,
            axis.home_position,
            axis.negative_target,
            config.search_timeout,
            config.home_timeout,
            config.move_timeout,
            config.motion_start_timeout,
            config.poll_period,
            config.stall_seconds,
            config.stall_tolerance,
            config.settle_seconds,
        )

        await self.busy.write(1)
        await self.active_axis.write(axis_name)
        await self._write_message(HomeState.CHECKING, f"{axis_name}: starting")

        def progress(state: HomeState, message: str) -> None:
            asyncio.run_coroutine_threadsafe(self._write_message(state, message), loop)

        try:
            LOG.warning("home starting for %s", axis_name)
            await asyncio.to_thread(
                homer.home_axis,
                axis,
                abort_event=self._abort_event,
                progress=progress,
            )
            LOG.warning("home routine returned successfully for %s", axis_name)
        except Exception as exc:
            LOG.exception("home routine raised for %s", axis_name)
            await self._write_error(str(exc))
        finally:
            LOG.warning(
                "home finished for %s; disarming IOC current status=%r message=%r last_error=%r",
                axis_name,
                self.status.value,
                self.message.value,
                self.last_error.value,
            )
            await self.busy.write(0)
            await self.active_axis.write("")
            await self.arm.write(0)
            LOG.warning(
                "home cleanup complete for %s arm=%r busy=%r active_axis=%r status=%r message=%r",
                axis_name,
                self.arm.value,
                self.busy.value,
                self.active_axis.value,
                self.status.value,
                self.message.value,
            )
            await asyncio.to_thread(
                homer.monitor_axis,
                axis,
                "post-home",
                duration=float(self.post_home_monitor_seconds.value),
                period=max(float(self.poll_period.value), 0.25),
            )

    async def _write_message(self, state: HomeState, message: str) -> None:
        await self.status.write(state.value)
        await self.status_code.write(HOME_STATE_CODES[state])
        await self.message.write(message)

    async def _write_error(self, message: str) -> None:
        await self.status.write(HomeState.ERROR.value)
        await self.status_code.write(HOME_STATE_CODES[HomeState.ERROR])
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
    log_epics_network_config(run_options)
    prefix = ioc_options["prefix"]
    ioc = SmaractHomeIOC(prefix=prefix)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
