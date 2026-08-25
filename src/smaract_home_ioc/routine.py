from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from enum import Enum
from threading import Event
from typing import Callable, Protocol


LOG = logging.getLogger(__name__)

SNAPSHOT_FIELDS = (
    "VAL",
    "RBV",
    "DVAL",
    "DRBV",
    "RVAL",
    "RRBV",
    "DMOV",
    "MOVN",
    "MSTA",
    "SET",
    "FOFF",
    "OFF",
    "LLS",
    "HLS",
    "LVIO",
    "LLM",
    "HLM",
    "RDBD",
    "HOMF",
    "HOMR",
    "STOP",
    "CNEN",
    "STAT",
    "SEVR",
)
SNAPSHOT_TIMEOUT = 0.2


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


HOME_STATE_CODES = {
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
    soft_limit_margin: float = 1.0


class HomingAborted(RuntimeError):
    """Raised when an operator aborts the routine."""


def _fmt_position(position: float) -> str:
    return f"{position:.12g}"


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
        low_soft_limit = None

        try:
            self._log_phase(axis, "home_axis start")
            self._check_ready(axis, abort_event, progress)
            low_soft_limit = self._lower_blocking_low_soft_limit(axis, progress)
            self._find_negative_limit(axis, abort_event, progress)
            self._home_forward(axis, abort_event, progress)
            self._set_current_position(axis, abort_event, progress)
            self._move_to_zero(axis, abort_event, progress)
        except HomingAborted:
            self._log_phase(axis, "home_axis aborted; stopping")
            self._stop(axis)
            self._restore_low_soft_limit(axis, low_soft_limit, progress, suppress_errors=True)
            progress(HomeState.ABORTED, f"{axis.name}: homing aborted")
            return HomeState.ABORTED
        except Exception as exc:
            LOG.exception("%s: home_axis failed", axis.name)
            self._log_phase(axis, "home_axis error; stopping")
            self._stop(axis)
            self._restore_low_soft_limit(axis, low_soft_limit, progress, suppress_errors=True)
            progress(HomeState.ERROR, f"{axis.name}: {exc}")
            raise

        self._restore_low_soft_limit(axis, low_soft_limit, progress)
        self._log_phase(axis, "home_axis complete")
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

    def _get_raw_for_log(self, pv: str):
        try:
            get_for_log = getattr(self.client, "get_for_log", None)
            if get_for_log is not None:
                return get_for_log(pv, timeout=SNAPSHOT_TIMEOUT)
            return self.client.get(pv, timeout=SNAPSHOT_TIMEOUT)
        except Exception as exc:
            return f"<{type(exc).__name__}: {exc}>"

    def _motor_snapshot(self, axis: AxisConfig) -> str:
        values = []
        for field in SNAPSHOT_FIELDS:
            value = self._get_raw_for_log(self._pv(axis, field))
            values.append(f"{field}={value!r}")
        return ", ".join(values)

    def _log_snapshot(self, axis: AxisConfig, label: str) -> None:
        LOG.warning("%s snapshot: %s", label, self._motor_snapshot(axis))

    def _log_phase(self, axis: AxisConfig, phase: str) -> None:
        LOG.warning("%s: %s", axis.name, phase)
        self._log_snapshot(axis, f"{axis.name}: {phase}")

    def monitor_axis(self, axis: AxisConfig, label: str, *, duration: float, period: float) -> None:
        if duration <= 0:
            return
        period = max(period, 0.1)
        start = time.monotonic()
        deadline = start + duration
        LOG.warning(
            "%s: %s monitor start duration=%s period=%s",
            axis.name,
            label,
            duration,
            period,
        )
        while time.monotonic() < deadline:
            LOG.warning(
                "%s: %s monitor elapsed=%.3fs snapshot: %s",
                axis.name,
                label,
                time.monotonic() - start,
                self._motor_snapshot(axis),
            )
            time.sleep(period)
        LOG.warning("%s: %s monitor complete", axis.name, label)

    def _put(
        self,
        pv: str,
        value,
        *,
        wait: bool = False,
        timeout: float | None = None,
        axis: AxisConfig | None = None,
    ) -> None:
        LOG.warning("motor command: caput %s=%r wait=%s", pv, value, wait)
        if axis is not None:
            self._log_snapshot(axis, f"{axis.name}: before caput {pv}={value!r}")
        self.client.put(pv, value, wait=wait, timeout=timeout or self.config.put_timeout)
        if axis is not None:
            self._log_snapshot(axis, f"{axis.name}: after caput {pv}={value!r}")

    def _check_ready(self, axis: AxisConfig, abort_event: Event, progress: ProgressCallback) -> None:
        progress(HomeState.CHECKING, f"{axis.name}: checking motor state")
        self._check_abort(abort_event, axis)
        self._log_phase(axis, "checking ready state")

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

    def low_soft_limit_warning(self, axis: AxisConfig) -> str | None:
        low_soft_limit = self._get_float(self._pv(axis, "LLM"))
        high_soft_limit = self._get_float(self._pv(axis, "HLM"))
        if self._soft_limits_are_unset(low_soft_limit, high_soft_limit):
            return None
        if low_soft_limit < axis.negative_target:
            return None
        temporary_limit = self._temporary_low_soft_limit(axis)
        return (
            f"{axis.name}: low soft limit .LLM={_fmt_position(low_soft_limit)} is not below "
            f"NegativeTarget={_fmt_position(axis.negative_target)}; homing will temporarily lower .LLM "
            f"to {_fmt_position(temporary_limit)}"
        )

    def _temporary_low_soft_limit(self, axis: AxisConfig) -> float:
        return axis.negative_target - abs(self.config.soft_limit_margin)

    def _soft_limits_are_unset(self, low_soft_limit: float, high_soft_limit: float) -> bool:
        return low_soft_limit == 0.0 and high_soft_limit == 0.0

    def _lower_blocking_low_soft_limit(
        self,
        axis: AxisConfig,
        progress: ProgressCallback,
    ) -> float | None:
        self._log_phase(axis, "checking soft limits")
        low_soft_limit = self._get_float(self._pv(axis, "LLM"))
        high_soft_limit = self._get_float(self._pv(axis, "HLM"))
        if self._soft_limits_are_unset(low_soft_limit, high_soft_limit):
            progress(
                HomeState.CHECKING,
                f"{axis.name}: soft limits are unset (.LLM=.HLM=0); leaving them unchanged",
            )
            return None
        if low_soft_limit < axis.negative_target:
            return None

        temporary_limit = self._temporary_low_soft_limit(axis)
        progress(
            HomeState.CHECKING,
            f"{axis.name}: temporarily lowering low soft limit .LLM from {_fmt_position(low_soft_limit)} "
            f"to {_fmt_position(temporary_limit)} so "
            f"NegativeTarget={_fmt_position(axis.negative_target)} is permitted",
        )
        self._put(self._pv(axis, "LLM"), temporary_limit, wait=True, axis=axis)
        return low_soft_limit

    def _restore_low_soft_limit(
        self,
        axis: AxisConfig,
        low_soft_limit: float | None,
        progress: ProgressCallback,
        *,
        suppress_errors: bool = False,
    ) -> None:
        if low_soft_limit is None:
            return

        try:
            self._log_phase(axis, "restoring low soft limit")
            progress(
                HomeState.CHECKING,
                f"{axis.name}: restoring low soft limit .LLM to {_fmt_position(low_soft_limit)}",
            )
            self._put(self._pv(axis, "LLM"), low_soft_limit, wait=True, axis=axis)
        except Exception as exc:
            if not suppress_errors:
                raise
            progress(HomeState.ERROR, f"{axis.name}: failed to restore low soft limit .LLM: {exc}")

    def _find_negative_limit(self, axis: AxisConfig, abort_event: Event, progress: ProgressCallback) -> None:
        self._log_phase(axis, "find negative limit start")
        progress(
            HomeState.FINDING_NEGATIVE_LIMIT,
            f"{axis.name}: moving toward negative stop at {axis.negative_target}",
        )
        self._put(self._pv(axis, "VAL"), axis.negative_target, wait=False, axis=axis)

        deadline = time.monotonic() + self.config.search_timeout
        motion_start_deadline = time.monotonic() + min(
            self.config.search_timeout,
            self.config.motion_start_timeout,
        )
        last_rbv = self._get_float(self._pv(axis, "RBV"))
        last_motion_time = time.monotonic()
        saw_motion = False

        while time.monotonic() < deadline:
            self._check_abort(abort_event, axis)
            time.sleep(self.config.poll_period)

            if self._get_int(self._pv(axis, "LLS"), default=0) != 0 or self._get_int(
                self._pv(axis, "HLS"), default=0
            ) != 0:
                self._log_phase(axis, "limit flag detected during negative search")
                self._stop(axis)
                time.sleep(self.config.settle_seconds)
                self._log_phase(axis, "negative search stopped after limit flag")
                progress(HomeState.FINDING_NEGATIVE_LIMIT, f"{axis.name}: limit flag detected")
                return

            dmov = self._get_int(self._pv(axis, "DMOV"), default=0)
            movn = self._get_int(self._pv(axis, "MOVN"), default=0)
            if movn != 0 or dmov == 0:
                if not saw_motion:
                    LOG.warning(
                        "%s: negative search motion observed dmov=%s movn=%s rbv=%s",
                        axis.name,
                        dmov,
                        movn,
                        self._get_raw_for_log(self._pv(axis, "RBV")),
                    )
                saw_motion = True

            rbv = self._get_float(self._pv(axis, "RBV"))
            if abs(rbv - last_rbv) > self.config.stall_tolerance:
                saw_motion = True
                last_rbv = rbv
                last_motion_time = time.monotonic()
                continue

            if saw_motion and time.monotonic() - last_motion_time >= self.config.stall_seconds:
                self._log_phase(axis, "negative search stall detected")
                self._stop(axis)
                time.sleep(self.config.settle_seconds)
                self._log_phase(axis, "negative search stopped after stall")
                progress(HomeState.FINDING_NEGATIVE_LIMIT, f"{axis.name}: negative stop detected")
                return

            if saw_motion and dmov == 1 and movn == 0:
                self._log_phase(axis, "negative target move completed")
                progress(HomeState.FINDING_NEGATIVE_LIMIT, f"{axis.name}: negative move completed")
                return

            if not saw_motion and time.monotonic() >= motion_start_deadline:
                self._log_phase(axis, "negative target move did not start")
                self._stop(axis)
                raise RuntimeError(f"{axis.name}: negative move did not start")

        raise RuntimeError(f"{axis.name}: timed out while finding negative stop")

    def _home_forward(self, axis: AxisConfig, abort_event: Event, progress: ProgressCallback) -> None:
        self._log_phase(axis, "home-forward start")
        progress(HomeState.FINDING_HOME, f"{axis.name}: starting motor-record home-forward")
        self._put(self._pv(axis, "HOMF"), 1, wait=False, axis=axis)
        self._wait_done(
            axis,
            abort_event,
            self.config.home_timeout,
            HomeState.FINDING_HOME,
            require_motion=True,
        )

    def _set_current_position(self, axis: AxisConfig, abort_event: Event, progress: ProgressCallback) -> None:
        self._check_abort(abort_event, axis)
        self._log_phase(axis, "set current position start")
        progress(
            HomeState.SETTING_POSITION,
            f"{axis.name}: setting current user position to {axis.home_position}",
        )
        try:
            self._put(self._pv(axis, "SET"), 1, wait=True, axis=axis)
            self._put(self._pv(axis, "VAL"), axis.home_position, wait=True, axis=axis)
        finally:
            self._put(self._pv(axis, "SET"), 0, wait=True, axis=axis)
        self._wait_done(
            axis,
            abort_event,
            self.config.move_timeout,
            HomeState.SETTING_POSITION,
            target=axis.home_position,
        )
        time.sleep(self.config.settle_seconds)
        self._log_phase(axis, "set current position complete")

    def _move_to_zero(self, axis: AxisConfig, abort_event: Event, progress: ProgressCallback) -> None:
        self._log_phase(axis, "move to zero start")
        progress(HomeState.MOVING_TO_ZERO, f"{axis.name}: moving to new 0")
        self._put(self._pv(axis, "VAL"), 0.0, wait=False, axis=axis)
        self._wait_done(
            axis,
            abort_event,
            self.config.move_timeout,
            HomeState.MOVING_TO_ZERO,
            target=0.0,
        )
        self._log_phase(axis, "move to zero complete")

    def _wait_done(
        self,
        axis: AxisConfig,
        abort_event: Event,
        timeout: float,
        state: HomeState,
        *,
        target: float | None = None,
        require_motion: bool = False,
    ) -> None:
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        next_snapshot = started
        last_status = None
        saw_motion = False
        LOG.warning(
            "%s: wait_done start state=%s timeout=%s target=%r require_motion=%s",
            axis.name,
            state.value,
            timeout,
            target,
            require_motion,
        )
        self._log_snapshot(axis, f"{axis.name}: wait_done {state.value} initial")
        while time.monotonic() < deadline:
            self._check_abort(abort_event, axis)
            dmov = self._get_int(self._pv(axis, "DMOV"), default=1)
            movn = self._get_int(self._pv(axis, "MOVN"), default=0)
            rbv = self._get_raw_for_log(self._pv(axis, "RBV"))
            val = self._get_raw_for_log(self._pv(axis, "VAL"))
            msta = self._get_raw_for_log(self._pv(axis, "MSTA"))
            lls = self._get_raw_for_log(self._pv(axis, "LLS"))
            hls = self._get_raw_for_log(self._pv(axis, "HLS"))
            status = (dmov, movn, rbv, val, msta, lls, hls, saw_motion)

            if status != last_status:
                LOG.warning(
                    "%s: wait_done state=%s elapsed=%.3fs dmov=%s movn=%s rbv=%r val=%r "
                    "msta=%r lls=%r hls=%r saw_motion=%s",
                    axis.name,
                    state.value,
                    time.monotonic() - started,
                    dmov,
                    movn,
                    rbv,
                    val,
                    msta,
                    lls,
                    hls,
                    saw_motion,
                )
                last_status = status

            if time.monotonic() >= next_snapshot:
                self._log_snapshot(axis, f"{axis.name}: wait_done {state.value} periodic")
                next_snapshot = time.monotonic() + max(self.config.poll_period * 10, 1.0)

            if movn != 0 or dmov == 0:
                if not saw_motion:
                    LOG.warning(
                        "%s: wait_done state=%s observed motion at %.3fs",
                        axis.name,
                        state.value,
                        time.monotonic() - started,
                    )
                saw_motion = True
            elif dmov == 1:
                # Motor reports settled (DMOV=1, MOVN=0).
                if saw_motion:
                    LOG.warning(
                        "%s: wait_done state=%s complete after observed motion elapsed=%.3fs",
                        axis.name,
                        state.value,
                        time.monotonic() - started,
                    )
                    self._log_snapshot(axis, f"{axis.name}: wait_done {state.value} complete")
                    time.sleep(self.config.settle_seconds)
                    return
                # No motion observed yet. Only accept completion if this is a
                # genuine no-op, i.e. the readback is already at the commanded
                # target. Never assume "done" just because nothing moved for a
                # while: HOMF and slow controllers can take seconds to start,
                # and returning early lets the routine finish and disarm while
                # the real move is still pending.
                if not require_motion and target is not None:
                    rbv = self._get_float(self._pv(axis, "RBV"), default=target)
                    deadband = self._motion_deadband(axis)
                    error = abs(rbv - target)
                    LOG.warning(
                        "%s: wait_done state=%s no-motion target check rbv=%s target=%s "
                        "error=%s deadband=%s",
                        axis.name,
                        state.value,
                        rbv,
                        target,
                        error,
                        deadband,
                    )
                    if error <= deadband:
                        LOG.warning(
                            "%s: wait_done state=%s complete as no-op at target elapsed=%.3fs",
                            axis.name,
                            state.value,
                            time.monotonic() - started,
                        )
                        self._log_snapshot(axis, f"{axis.name}: wait_done {state.value} no-op complete")
                        time.sleep(self.config.settle_seconds)
                        return

            time.sleep(self.config.poll_period)
        self._log_snapshot(axis, f"{axis.name}: wait_done {state.value} timeout")
        raise RuntimeError(f"{axis.name}: timed out in {state.value}")

    def _motion_deadband(self, axis: AxisConfig) -> float:
        rdbd = self._get_float(self._pv(axis, "RDBD"), default=0.0)
        return max(abs(rdbd), abs(self.config.stall_tolerance))

    def _stop(self, axis: AxisConfig) -> None:
        try:
            self._put(self._pv(axis, "STOP"), 1, wait=False, timeout=2.0, axis=axis)
        except Exception:
            pass
