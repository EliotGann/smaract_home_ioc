from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from threading import Event
from typing import Callable, Protocol


class HomeState(str, Enum):
    IDLE = "idle"
    CHECKING = "checking"
    FINDING_NEGATIVE_LIMIT = "finding_negative_limit"
    FINDING_HOME = "finding_home"
    SETTING_POSITION = "setting_position"
    MOVING_TO_ZERO = "moving_to_zero"
    HOMED = "homed"
    ABORTED = "aborted"
    ERROR = "error"


class PVClient(Protocol):
    def get(self, pv: str, *, timeout: float = 2.0): ...

    def put(self, pv: str, value, *, wait: bool = False, timeout: float = 10.0) -> None: ...


ProgressCallback = Callable[[HomeState, str], None]


@dataclass(frozen=True)
class AxisConfig:
    name: str
    motor_prefix: str
    home_position: float
    negative_target: float


@dataclass(frozen=True)
class RoutineConfig:
    search_timeout: float = 300.0
    home_timeout: float = 120.0
    move_timeout: float = 120.0
    motion_start_timeout: float = 5.0
    poll_period: float = 0.25
    stall_seconds: float = 5.0
    stall_tolerance: float = 0.002
    settle_seconds: float = 1.0
    put_timeout: float = 10.0


class HomingAborted(RuntimeError):
    """Raised when an operator aborts the routine."""


class SmaractHomer:
    def __init__(self, client: PVClient, config: RoutineConfig | None = None):
        self.client = client
        self.config = config or RoutineConfig()

    def home_axis(
        self,
        axis: AxisConfig,
        *,
        abort_event: Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> HomeState:
        abort_event = abort_event or Event()
        progress = progress or (lambda state, message: None)

        try:
            self._check_ready(axis, abort_event, progress)
            self._find_negative_limit(axis, abort_event, progress)
            self._home_forward(axis, abort_event, progress)
            self._set_current_position(axis, abort_event, progress)
            self._move_to_zero(axis, abort_event, progress)
        except HomingAborted:
            self._stop(axis)
            progress(HomeState.ABORTED, f"{axis.name}: homing aborted")
            return HomeState.ABORTED
        except Exception as exc:
            self._stop(axis)
            progress(HomeState.ERROR, f"{axis.name}: {exc}")
            raise

        progress(HomeState.HOMED, f"{axis.name}: homed and moved to 0")
        return HomeState.HOMED

    def _pv(self, axis: AxisConfig, field: str) -> str:
        return f"{axis.motor_prefix}.{field}"

    def _check_abort(self, abort_event: Event, axis: AxisConfig) -> None:
        if abort_event.is_set():
            self._stop(axis)
            raise HomingAborted()

    def _get_float(self, pv: str, *, default=None) -> float:
        value = self.client.get(pv, timeout=self.config.put_timeout)
        if value is None:
            if default is not None:
                return default
            raise RuntimeError(f"could not read {pv}")
        return float(value)

    def _get_int(self, pv: str, *, default=None) -> int:
        value = self.client.get(pv, timeout=self.config.put_timeout)
        if value is None:
            if default is not None:
                return default
            raise RuntimeError(f"could not read {pv}")
        return int(value)

    def _put(self, pv: str, value, *, wait: bool = False, timeout: float | None = None) -> None:
        self.client.put(pv, value, wait=wait, timeout=timeout or self.config.put_timeout)

    def _check_ready(self, axis: AxisConfig, abort_event: Event, progress: ProgressCallback) -> None:
        progress(HomeState.CHECKING, f"{axis.name}: checking motor state")
        self._check_abort(abort_event, axis)

        if self._get_int(self._pv(axis, "CNEN"), default=1) != 1:
            raise RuntimeError(f"{axis.name}: motor is not enabled (.CNEN != 1)")
        if self._get_int(self._pv(axis, "LVIO"), default=0) != 0:
            raise RuntimeError(f"{axis.name}: motor has a limit violation (.LVIO != 0)")
        if self._get_int(self._pv(axis, "SEVR"), default=0) != 0:
            raise RuntimeError(f"{axis.name}: motor alarm severity is non-zero")
        if self._get_int(self._pv(axis, "DMOV"), default=1) != 1:
            raise RuntimeError(f"{axis.name}: motor is not done moving (.DMOV != 1)")
        if self._get_int(self._pv(axis, "MOVN"), default=0) != 0:
            raise RuntimeError(f"{axis.name}: motor is moving (.MOVN != 0)")
        if self._get_int(self._pv(axis, "HLS"), default=0) != 0:
            raise RuntimeError(f"{axis.name}: high-limit flag is active before homing")
        if self._get_int(self._pv(axis, "LLS"), default=0) != 0:
            raise RuntimeError(f"{axis.name}: low-limit flag is active before homing")

    def _find_negative_limit(self, axis: AxisConfig, abort_event: Event, progress: ProgressCallback) -> None:
        progress(
            HomeState.FINDING_NEGATIVE_LIMIT,
            f"{axis.name}: moving toward negative stop at {axis.negative_target}",
        )
        self._put(self._pv(axis, "VAL"), axis.negative_target, wait=False)

        deadline = time.monotonic() + self.config.search_timeout
        last_rbv = self._get_float(self._pv(axis, "RBV"))
        last_motion_time = time.monotonic()

        while time.monotonic() < deadline:
            self._check_abort(abort_event, axis)
            time.sleep(self.config.poll_period)
            rbv = self._get_float(self._pv(axis, "RBV"))
            if abs(rbv - last_rbv) > self.config.stall_tolerance:
                last_rbv = rbv
                last_motion_time = time.monotonic()
                continue

            if time.monotonic() - last_motion_time >= self.config.stall_seconds:
                self._stop(axis)
                time.sleep(self.config.settle_seconds)
                progress(HomeState.FINDING_NEGATIVE_LIMIT, f"{axis.name}: negative stop detected")
                return

            if self._get_int(self._pv(axis, "DMOV"), default=0) == 1 and self._get_int(
                self._pv(axis, "MOVN"), default=0
            ) == 0:
                progress(HomeState.FINDING_NEGATIVE_LIMIT, f"{axis.name}: negative move completed")
                return

        raise RuntimeError(f"{axis.name}: timed out while finding negative stop")

    def _home_forward(self, axis: AxisConfig, abort_event: Event, progress: ProgressCallback) -> None:
        progress(HomeState.FINDING_HOME, f"{axis.name}: starting motor-record home-forward")
        self._put(self._pv(axis, "HOMF"), 1, wait=False)
        self._wait_done(axis, abort_event, self.config.home_timeout, HomeState.FINDING_HOME)

    def _set_current_position(self, axis: AxisConfig, abort_event: Event, progress: ProgressCallback) -> None:
        self._check_abort(abort_event, axis)
        progress(
            HomeState.SETTING_POSITION,
            f"{axis.name}: setting current user position to {axis.home_position}",
        )
        self._put(self._pv(axis, "SET"), 1, wait=True)
        self._put(self._pv(axis, "VAL"), axis.home_position, wait=True)
        self._put(self._pv(axis, "SET"), 0, wait=True)
        time.sleep(self.config.settle_seconds)

    def _move_to_zero(self, axis: AxisConfig, abort_event: Event, progress: ProgressCallback) -> None:
        progress(HomeState.MOVING_TO_ZERO, f"{axis.name}: moving to new 0")
        self._put(self._pv(axis, "VAL"), 0.0, wait=False)
        self._wait_done(axis, abort_event, self.config.move_timeout, HomeState.MOVING_TO_ZERO)

    def _wait_done(
        self,
        axis: AxisConfig,
        abort_event: Event,
        timeout: float,
        state: HomeState,
    ) -> None:
        deadline = time.monotonic() + timeout
        motion_start_deadline = time.monotonic() + min(timeout, self.config.motion_start_timeout)
        saw_motion = False
        while time.monotonic() < deadline:
            self._check_abort(abort_event, axis)
            dmov = self._get_int(self._pv(axis, "DMOV"), default=1)
            movn = self._get_int(self._pv(axis, "MOVN"), default=0)
            if movn != 0 or dmov == 0:
                saw_motion = True
            if saw_motion and dmov == 1 and movn == 0:
                time.sleep(self.config.settle_seconds)
                return
            if not saw_motion and dmov == 1 and movn == 0 and time.monotonic() >= motion_start_deadline:
                time.sleep(self.config.settle_seconds)
                return
            time.sleep(self.config.poll_period)
        raise RuntimeError(f"{axis.name}: timed out in {state.value}")

    def _stop(self, axis: AxisConfig) -> None:
        try:
            self._put(self._pv(axis, "STOP"), 1, wait=False, timeout=2.0)
        except Exception:
            pass
