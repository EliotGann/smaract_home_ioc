# smaract-home-ioc

Caproto IOC for repeatable homing of selected SMI SmarAct motors.

The IOC does not modify the SmarAct IOC. It commands the existing motor records:

- `piezo.x`: `XF:12ID2C-ES{MCS:2-Ax:3}Mtr`
- WAXS beamstop X: `XF:12ID2C-ES{MCS:2-Ax:1}Mtr`

## Routine

For one axis at a time:

1. Require `Arm=1` and verify the motor is enabled, idle, not in limit violation, and not in alarm.
2. Move toward a configurable negative target until a motor-record limit flag lights.
3. Stop the motor after detecting the limit flag. Stall detection remains as a fallback.
4. Run the motor-record home-forward command with `.HOMF`.
5. Put the motor record in set mode, set the current user position to the configured home position, and exit set mode.
6. Move the axis to the new user position `0`.

Defaults:

- `X:HomePosition = -50000`
- `WBSX:HomePosition = 0`
- `X:NegativeTarget = -1000000`
- `WBSX:NegativeTarget = -1000000`

The negative target and stall-detection parameters should be tuned carefully during local testing.

## Run Locally

```bash
pixi run ioc
```

If local workstation networking blocks EPICS broadcast beacons, run with the local task:

```bash
pixi run ioc-local --prefix XF:12IDC-ES:SMARACT_HOME_TEST:
```

This uses `EPICS_CAS_BEACON_ADDR_LIST=10.67.91.255`, matching the SMI workstation CA broadcast address.

The default IOC prefix is:

```text
XF:12IDC-ES:SMARACT_HOME:
```

Override it with either:

```bash
SMARACT_HOME_IOC_PREFIX='XF:12IDC-ES:SMARACT_HOME_TEST:' pixi run ioc
```

or caproto's normal prefix option:

```bash
pixi run ioc --prefix XF:12IDC-ES:SMARACT_HOME_TEST:
```

## Useful PVs

- `XF:12IDC-ES:SMARACT_HOME:Arm`: set to `1` before each routine.
- `XF:12IDC-ES:SMARACT_HOME:HomeX`: write `1` to home `x`.
- `XF:12IDC-ES:SMARACT_HOME:HomeWBSX`: write `1` to home WAXS beamstop X.
- `XF:12IDC-ES:SMARACT_HOME:Abort`: write `1` to request abort.
- `XF:12IDC-ES:SMARACT_HOME:Busy`: `1` while a routine is active.
- `XF:12IDC-ES:SMARACT_HOME:ActiveAxis`: active axis name.
- `XF:12IDC-ES:SMARACT_HOME:Status`: current state.
- `XF:12IDC-ES:SMARACT_HOME:Message`: current detail message.
- `XF:12IDC-ES:SMARACT_HOME:LastError`: last error message.
- `XF:12IDC-ES:SMARACT_HOME:X:HomePosition`: current-position value applied after `x` home-forward.
- `XF:12IDC-ES:SMARACT_HOME:WBSX:HomePosition`: current-position value applied after WAXS beamstop X home-forward.
- `XF:12IDC-ES:SMARACT_HOME:X:NegativeTarget`: target used to search negative stop for `x`.
- `XF:12IDC-ES:SMARACT_HOME:WBSX:NegativeTarget`: target used to search negative stop for WAXS beamstop X.
- `XF:12IDC-ES:SMARACT_HOME:StallSeconds`: unchanged-readback time required to call the negative stop reached.
- `XF:12IDC-ES:SMARACT_HOME:StallTolerance`: readback change below this is treated as unchanged.
- `XF:12IDC-ES:SMARACT_HOME:MotionStartTimeout`: grace period for `.DMOV/.MOVN` to show motion before accepting an already-done command.

## Test

```bash
pixi run test
```

The unit tests exercise the homing state machine with a fake PV client. They do not connect to EPICS and do not move hardware.

## First Hardware Test Suggestion

Use a test prefix first, check all configurable PVs, and start with conservative negative targets near the current position before increasing travel.

Example:

```bash
SMARACT_HOME_IOC_PREFIX='XF:12IDC-ES:SMARACT_HOME_TEST:' pixi run ioc
```

Then adjust `X:NegativeTarget`, `WBSX:NegativeTarget`, `StallSeconds`, and `StallTolerance` before triggering `HomeX` or `HomeWBSX`.
