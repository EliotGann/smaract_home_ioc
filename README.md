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

## Motion-completion logic

Each commanded motor step (negative search, home-forward, set-position, move-to-zero) waits for genuine confirmation before advancing:

- **Observed motion then settled** (`DMOV=1`, `MOVN=0` after `DMOV=0` or `MOVN=1` was seen): the primary completion signal for all steps.
- **Already at target (no-op)**: for set-position and move-to-zero, if `RBV` is already within the motor record deadband (`.RDBD`) of the commanded target the step is treated as a no-op and the routine advances immediately.
- **Home-forward (`HOMF`) requires observed motion**: `HOMF` must cause `DMOV=0` or `MOVN=1` before the routine declares the referencing done. If the controller never starts moving within `HomeTimeout`, the routine errors and stops. This prevents the routine from silently racing ahead while the real referencing move runs after the IOC reports homed.
- **Negative-search motion guard**: the initial negative move must show motion within `MotionStartTimeout`. If it does not, the routine errors and stops rather than declaring the negative stop immediately reached.

## Known external interaction with c3mc9

The `c3mc9` IOC (`/epics/iocs/c3mc9`) monitors `XF:12IDC-ES:SMARACT_HOME:StatusCode` and uses a transition to code `6` (homed) as a gate to start continuous position tracking of `XF:12ID2C-ES{MCS:2-Ax:3}Mtr` via `platform_move_x_axis.db`. This is intentional for sample-exchange workflows initiated by `c3mc9`, but it also fires when any other client or operator homes the axis independently.

If unexpected negative motion of Ax:3 occurs after a manual or maintenance homing, check whether `c3mc9` is running and whether its `xf12id2_smaract_homed` latch became non-zero. See the bug report in the `c3mc9` repository for the recommended fix (own-workflow latch).

## Logging

The IOC logs at `WARNING` level throughout the homing sequence. Key log lines:

- `HomeX put value=...` / `Arm put value=...`: every write to a control PV, with current arm/busy/axis state.
- `home requested for ...`: entry into `_start_home`, with task state.
- `home run configured ...`: full axis and timing configuration at start.
- `home starting for ...` / `home routine returned successfully for ...` / `home finished for ...`: routine lifecycle.
- `motor command: caput ...`: every motor record write this IOC issues.
- `... before/after caput ...` snapshots: full motor record snapshot (`VAL`, `RBV`, `DMOV`, `MOVN`, `MSTA`, `SET`, `OFF`, `LLS`, `HLS`, `LVIO`, `LLM`, `HLM`, `HOMF`, `HOMR`, etc.) before and after each command.
- `... wait_done state=... elapsed=...`: every status change during each wait loop.
- `post-home monitor elapsed=...`: motor snapshots for `PostHomeMonitorSeconds` seconds after disarm, to catch any unexpected post-completion motion.

## Run Locally

```bash
pixi run ioc
```

On IOC hosts, startup reads EPICS environment settings from:

```text
/epics/common/xf12id2-inst-ioc1-netsetup.cmd
```

Any `epicsEnvSet("NAME", "VALUE")` lines in that file are applied before caproto
starts, unless `NAME` is already set in the process environment.

Network addresses belong in the host's netsetup file or process environment, not
in this repository. Configure client discovery with `EPICS_CA_ADDR_LIST` and
`EPICS_CA_AUTO_ADDR_LIST`, and server beacons with `EPICS_CAS_BEACON_ADDR_LIST`
and `EPICS_CAS_AUTO_BEACON_ADDR_LIST` as appropriate for your deployment.

The former `ioc-local` task has been removed because it embedded a site-specific
beacon destination. For that workstation workaround, set
`EPICS_CAS_BEACON_ADDR_LIST` to your deployment's beacon destination and
`EPICS_CAS_AUTO_BEACON_ADDR_LIST=NO` in the environment, then use the normal task:

```bash
pixi run ioc --prefix XF:12IDC-ES:SMARACT_HOME_TEST:
```

Server bind interfaces follow caproto's `--interfaces` option or
`EPICS_CAS_INTF_ADDR_LIST`. Without an explicit interface setting, caproto defaults
to listening on all IPv4 interfaces, not just localhost. Running on a workstation
or using a test PV prefix does not isolate the IOC from the network or hardware.

### Broadcast Discovery Troubleshooting

With caproto 1.3.0 on this Linux host, binding to the unicast address from
`EPICS_CAS_INTF_ADDR_LIST` does not receive subnet-broadcast CA searches. A
temporary workaround is an explicit wildcard bind:

```bash
pixi run ioc --interfaces 0.0.0.0
```

The deployed `/epics/iocs/MCS02HOMING/st.cmd` currently uses this override. It
listens on all IPv4 interfaces; it does not change the configured beacon
destinations. Remove `--interfaces 0.0.0.0` from that launcher to undo the
workaround. Do not run a second instance with the production PV prefix while
the service is running.

For a read-only discovery check, use `caget -w 3
'XF:12IDC-ES:SMARACT_HOME:Status'` with `EPICS_CA_AUTO_ADDR_LIST=NO` and
`EPICS_CA_ADDR_LIST` set to the instrument subnet broadcast address. A unicast
search to the host IP is not a reliable substitute when multiple IOCs share
UDP port 5064. Beacons do not advertise a list of PV names.

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
- `XF:12IDC-ES:SMARACT_HOME:StatusCode`: numeric state: `0` idle, `1` checking, `2` finding_negative_limit, `3` finding_home, `4` setting_position, `5` moving_to_zero, `6` homed, `7` aborted, `8` error.
- `XF:12IDC-ES:SMARACT_HOME:Message`: current detail message.
- `XF:12IDC-ES:SMARACT_HOME:LastError`: last error message.
- `XF:12IDC-ES:SMARACT_HOME:X:HomePosition`: current-position value applied after `x` home-forward.
- `XF:12IDC-ES:SMARACT_HOME:WBSX:HomePosition`: current-position value applied after WAXS beamstop X home-forward.
- `XF:12IDC-ES:SMARACT_HOME:X:NegativeTarget`: target used to search negative stop for `x`.
- `XF:12IDC-ES:SMARACT_HOME:WBSX:NegativeTarget`: target used to search negative stop for WAXS beamstop X.
- `XF:12IDC-ES:SMARACT_HOME:StallSeconds`: unchanged-readback time required to call the negative stop reached.
- `XF:12IDC-ES:SMARACT_HOME:StallTolerance`: readback change below this is treated as unchanged.
- `XF:12IDC-ES:SMARACT_HOME:MotionStartTimeout`: grace period for `.DMOV/.MOVN` to show motion before erroring on a move that did not start.
- `XF:12IDC-ES:SMARACT_HOME:PostHomeMonitorSeconds`: how long to keep logging motor snapshots after disarm (default `20.0`; set to `0` to disable).

## CS-Studio Display

`opi/smaract_home.opi` is a standalone CS-Studio BOY display intended to live in an operator CSS workspace, not on the IOC server.

Default macros are included in the file:

- `P=XF:12IDC-ES:SMARACT_HOME:`
- `X_MOTOR=XF:12ID2C-ES{MCS:2-Ax:3}Mtr`
- `WBSX_MOTOR=XF:12ID2C-ES{MCS:2-Ax:1}Mtr`

Override these macros from CSS if using a test IOC prefix or alternate motor records. The display provides `Arm`, `HomeX`, `HomeWBSX`, and `Abort` controls, routine status/error readbacks, homing configuration fields, and live motor record indicators for readback, done, limits, limit violation, and controller enable.

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
