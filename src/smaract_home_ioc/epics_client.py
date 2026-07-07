from __future__ import annotations


class EpicsPVClient:
    """Small adapter around pyepics, isolated to keep the routine testable."""

    def __init__(self):
        import epics

        self._epics = epics

    def get(self, pv: str, *, timeout: float = 2.0):
        return self._epics.caget(pv, timeout=timeout)

    def put(self, pv: str, value, *, wait: bool = False, timeout: float = 10.0) -> None:
        ok = self._epics.caput(pv, value, wait=wait, timeout=timeout)
        if not ok:
            raise TimeoutError(f"caput failed or timed out: {pv}={value!r}")
