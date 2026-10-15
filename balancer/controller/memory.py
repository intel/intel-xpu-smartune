# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import errno as _errno
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, Optional

from controller.base import ControllerBase

from utils.logger import get_logger
logger = get_logger(__name__)
from utils.app_utils import write_cgroup_file
from config.config import b_config

# ---------------------------------------------------------------------------
# memory.reclaim swappiness
#
# Without an explicit swappiness the kernel takes the cheap route and reclaims
# *only* clean page cache -- not a single anonymous page moves.  For a NAS that
# is worse than doing nothing: the file-serving cache is destroyed and the
# memory pressure, which is anonymous, is untouched.  So every caller must pass
# a swappiness, and the default here is anon-biased.
#
# Even at swappiness=200 the kernel drains the remaining page cache before
# touching anon.  swappiness tunes the *scan ratio*, it is not an absolute
# preference -- there is no "anon only" mode.  Protecting page cache is
# therefore the caller's job (see file_cache_fraction()), not swappiness'.
# ---------------------------------------------------------------------------
DEFAULT_RECLAIM_SWAPPINESS = 180

# A bare systemd unit name.  Anything else is refused before it reaches os.walk
# or gets joined into a write path.
CGROUP_ID_RE = re.compile(r"[A-Za-z0-9._@:-]+")

# Stand-in for a rejected id.  The name cannot match CGROUP_ID_RE, so it can
# never collide with a real unit, and it keeps the returned path inside the
# mount instead of letting a traversal attempt out.
_NO_SUCH_CGROUP = "!invalid"


def _is_safe_cgroup_ref(cgroup: str) -> bool:
    """True for a bare unit name or a slash-separated path below the mount.

    Note that ``..`` matches CGROUP_ID_RE all on its own -- both dots are in the
    character class -- so traversal has to be excluded by name, not by pattern.
    """
    if not cgroup:
        return False
    return all(segment not in (".", "..") and CGROUP_ID_RE.fullmatch(segment)
               for segment in cgroup.split("/"))

# The kernel accepts 0..200 inclusive; 201 and non-numeric values are rejected
# with EINVAL (measured).
SWAPPINESS_MIN = 0
SWAPPINESS_MAX = 200


@dataclass
class ReclaimOutcome:
    """Result of one ``memory.reclaim`` write.

    ``ok`` only means the kernel accepted the request and reported no error.
    ``reclaimed_bytes`` is measured from ``memory.current`` either side of the
    write, because the kernel reports nothing about how much it actually freed
    and may return EAGAIN *after* having reclaimed part of the request.
    """
    ok: bool
    errno: Optional[int] = None
    requested_bytes: int = 0
    reclaimed_bytes: int = 0
    elapsed_ms: float = 0.0
    swappiness: Optional[int] = None
    reason: str = ""

    @property
    def is_eagain(self) -> bool:
        """True when the cgroup had no reclaim headroom left.

        Seen when swap is full or ``memory.swap.max=0`` -- the write returns in
        a few milliseconds having done nothing.  The caller should back off and
        grow swap rather than retry immediately.
        """
        return self.errno == _errno.EAGAIN

    @property
    def unsupported(self) -> bool:
        """True when this kernel rejected the request outright (EINVAL/ENOENT)."""
        return self.errno in (_errno.EINVAL, _errno.ENOENT)


class MemoryController(ControllerBase):
    """cgroup v2 memory controller.

    Only reads and ``memory.reclaim`` live here.  ``memory.high`` stays on the
    ``systemctl set-property`` path in controller.py -- systemd owns that
    property and a second writer would silently drift from it.  systemd has no
    property for ``memory.reclaim``, so writing the cgroup file directly creates
    no such conflict.
    """

    def __init__(self, cgroup_mount: str):
        super().__init__(cgroup_mount)
        self._supports_swappiness: Optional[bool] = None
        self._path_cache: Dict[str, str] = {}
        self.set_managed_oom_pressure()

    def controller_type(self) -> str:
        return "memory"

    def _locate(self, cgroup: str) -> Optional[str]:
        """Search the v2 tree for the directory named *cgroup*.

        Same approach as Controller.find_cgroup_dir and io._get_full_cgroup_path:
        the unit name does not encode its own location, so the tree is searched.
        """
        if not CGROUP_ID_RE.fullmatch(cgroup):
            logger.warning(f"Rejecting invalid cgroup id for lookup: {cgroup!r}")
            return None
        for root, directories, _ in os.walk(self.cgroup_mount):
            if cgroup in directories:
                return os.path.join(root, cgroup)
        return None

    def get_full_path(self, cgroup: str) -> str:
        """Return the cgroup v2 path for *cgroup*.

        Overrides the inherited version, which is unusable here on two counts:
        it inserts the controller name (``/sys/fs/cgroup/memory/...``, the v1
        layout, absent under v2), and it expects a path relative to the mount
        while every caller here passes a bare unit name.  A systemd scope is
        nested -- a terminal's sits at
        ``user.slice/user-N.slice/user@N.service/app.slice/app-....slice/`` --
        so joining the mount to a bare name yields a path that never exists,
        turning every read into None and every reclaim into a silent no-op.

        Results are cached because the idle gate reads three files per candidate
        per tick and a tree walk per read would cost more than the reclaim.
        The cache is validated on use, so a re-created scope is re-located.
        """
        cgroup = cgroup.lstrip('/')
        if not _is_safe_cgroup_ref(cgroup):
            # Refuse before joining, not after: "../../etc" joined onto the
            # mount resolves to a real directory outside it, and exists() would
            # then happily report True for /etc.
            logger.warning(f"Rejecting invalid cgroup id: {cgroup!r}")
            return os.path.join(self.cgroup_mount, _NO_SUCH_CGROUP)

        if '/' in cgroup:
            # Already a path relative to the mount -- nothing to resolve.
            return os.path.join(self.cgroup_mount, cgroup)

        cached = self._path_cache.get(cgroup)
        if cached is not None:
            if os.path.isdir(cached):
                return cached
            del self._path_cache[cgroup]

        found = self._locate(cgroup)
        if found is None:
            # Nothing to resolve: hand back a path under the mount that cannot
            # exist, so callers read this as "this cgroup is gone" -- which by
            # now is the truthful answer -- without ever escaping the mount.
            return os.path.join(self.cgroup_mount, cgroup)
        self._path_cache[cgroup] = found
        return found

    # -- reads -------------------------------------------------------------

    def _read_int(self, cgroup: str, param: str) -> Optional[int]:
        raw = self.get_parameter(cgroup, param)
        if raw is None:
            return None
        try:
            return int(raw.strip())
        except ValueError:
            logger.debug(f"Non-integer {param} for {cgroup}: {raw!r}")
            return None

    def read_current(self, cgroup: str) -> Optional[int]:
        """Bytes charged to the cgroup (``memory.current``)."""
        return self._read_int(cgroup, "memory.current")

    def read_swap_current(self, cgroup: str) -> Optional[int]:
        """Bytes of this cgroup's memory currently in swap."""
        return self._read_int(cgroup, "memory.swap.current")

    def _read_flat_keyed(self, cgroup: str, param: str) -> Dict[str, int]:
        """Parse a ``key value`` per line cgroup file into a dict."""
        raw = self.get_parameter(cgroup, param)
        if raw is None:
            return {}
        out: Dict[str, int] = {}
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                continue
        return out

    def read_memory_stat(self, cgroup: str) -> Dict[str, int]:
        """``memory.stat`` as a dict.  Keys of interest: ``anon``, ``file``, ``slab``."""
        return self._read_flat_keyed(cgroup, "memory.stat")

    def read_events(self, cgroup: str) -> Dict[str, int]:
        """``memory.events`` as a dict.

        The ``high`` counter is the signal for "memory.high is actually biting".
        Do not use ``memory.current`` for that: memory.high is a soft limit and
        current legitimately sits above it.
        """
        return self._read_flat_keyed(cgroup, "memory.events")

    def read_cpu_usec(self, cgroup: str) -> Optional[int]:
        """``cpu.stat``'s ``usage_usec``, used for the idle gate before reclaim.

        Reclaiming from an *active* target is wasted work: it faults the pages
        straight back in within seconds, and the caller paid a synchronous
        block for them.
        """
        return self._read_flat_keyed(cgroup, "cpu.stat").get("usage_usec")

    def file_cache_fraction(self, cgroup: str) -> Optional[float]:
        """Share of this cgroup's charged memory that is page cache, 0.0-1.0.

        The caller uses this to protect cache-heavy apps (a media server, a
        photo indexer).  swappiness cannot do it -- see the module comment.
        Returns None when memory.stat is unreadable.
        """
        stat = self.read_memory_stat(cgroup)
        if not stat:
            return None
        anon = stat.get("anon", 0)
        file_ = stat.get("file", 0)
        total = anon + file_
        if total <= 0:
            return 0.0
        return file_ / total

    def reclaimable_anon(self, cgroup: str) -> int:
        """Bytes of anonymous memory that could still be pushed to swap."""
        return max(0, self.read_memory_stat(cgroup).get("anon", 0))

    # -- memory.reclaim ----------------------------------------------------

    def supports_swappiness(self, cgroup: str) -> bool:
        """Probe (once) whether this kernel accepts ``memory.reclaim`` swappiness.

        The ``swappiness=`` extension landed in 6.4; older kernels reject the
        whole write with EINVAL.  The probe asks for one byte: EINVAL means the
        syntax is unsupported, while success *or* EAGAIN both mean it parsed.
        """
        if self._supports_swappiness is not None:
            return self._supports_swappiness
        try:
            self._write_reclaim(cgroup, f"1 swappiness={DEFAULT_RECLAIM_SWAPPINESS}")
            self._supports_swappiness = True
        except OSError as exc:
            # EAGAIN: syntax fine, just nothing to reclaim -- still supported.
            self._supports_swappiness = exc.errno == _errno.EAGAIN
        if not self._supports_swappiness:
            logger.warning(
                "Kernel rejected 'memory.reclaim swappiness='; reclaim will stay "
                "disabled. Without it the kernel reclaims page cache only, which "
                "hurts more than it helps."
            )
        return self._supports_swappiness

    def _write_reclaim(self, cgroup: str, payload: str) -> None:
        path = os.path.join(self.get_full_path(cgroup), "memory.reclaim")
        write_cgroup_file(payload, path)

    def reclaim(self, cgroup: str, nbytes: int,
                swappiness: Optional[int] = DEFAULT_RECLAIM_SWAPPINESS) -> ReclaimOutcome:
        """Proactively reclaim *nbytes* from *cgroup*, biased towards anon pages.

        Unlike memory.high this pushes the cgroup *below* its limit, and the cost
        lands on the caller (this thread blocks; measured ~1 ms per MB) rather
        than on the target.  Run it off the balancer tick.

        *swappiness* must be 0..200.  Passing None reproduces the kernel default,
        which reclaims page cache only -- allowed, but never what the balancer
        wants; see the module comment.
        """
        if nbytes <= 0:
            return ReclaimOutcome(ok=True, requested_bytes=0, reason="nothing requested")

        if swappiness is not None:
            if not (SWAPPINESS_MIN <= int(swappiness) <= SWAPPINESS_MAX):
                return ReclaimOutcome(
                    ok=False, errno=_errno.EINVAL, requested_bytes=nbytes,
                    swappiness=swappiness,
                    reason=f"swappiness must be {SWAPPINESS_MIN}..{SWAPPINESS_MAX}",
                )
            if not self.supports_swappiness(cgroup):
                return ReclaimOutcome(
                    ok=False, errno=_errno.EINVAL, requested_bytes=nbytes,
                    swappiness=swappiness, reason="kernel lacks swappiness= support",
                )

        nbytes = int(nbytes)
        payload = str(nbytes) if swappiness is None else f"{nbytes} swappiness={int(swappiness)}"

        before = self.read_current(cgroup)
        started = time.monotonic()
        try:
            self._write_reclaim(cgroup, payload)
            err = None
        except OSError as exc:
            err = exc
        elapsed_ms = (time.monotonic() - started) * 1000.0

        after = self.read_current(cgroup)
        # EAGAIN can arrive after a partial reclaim, so always measure the delta
        # instead of trusting the return code for "how much was freed".
        freed = max(0, (before - after)) if (before is not None and after is not None) else 0

        if err is None:
            logger.info(
                f"reclaim({cgroup}, {nbytes / 1048576:.0f} MB, swappiness={swappiness}) "
                f"freed {freed / 1048576:.0f} MB in {elapsed_ms:.0f} ms"
            )
            return ReclaimOutcome(ok=True, requested_bytes=nbytes, reclaimed_bytes=freed,
                                  elapsed_ms=elapsed_ms, swappiness=swappiness)

        outcome = ReclaimOutcome(ok=False, errno=err.errno, requested_bytes=nbytes,
                                 reclaimed_bytes=freed, elapsed_ms=elapsed_ms,
                                 swappiness=swappiness, reason=str(err))
        if outcome.is_eagain:
            # Out of reclaim headroom -- typically swap full.  Retrying now just
            # burns the caller's time; the fix is more swap.
            logger.info(
                f"reclaim({cgroup}) EAGAIN after {freed / 1048576:.0f} MB in "
                f"{elapsed_ms:.0f} ms -- no reclaim headroom (swap full?)"
            )
        else:
            logger.error(f"reclaim({cgroup}, {nbytes}) failed: {err}")
        return outcome

    # -- writes ------------------------------------------------------------

    def set_managed_oom_pressure(self, user_service: str = "user@1000.service", oom_pressure: str = "auto") -> bool:
        """Control of systemd OOM will take over in the Balancer, so set the default value from kill to auto"""
        try:
            # smartune runs as root, so call systemctl directly (no sudo prefix).
            cmd = [
                'systemctl', 'set-property', '--runtime',
                user_service, f'ManagedOOMMemoryPressure={oom_pressure}'
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return True
            logger.error(f"Failed to set ManagedOOMMemoryPressure on {user_service}: {result.stderr.strip()}")
            return False
        except Exception:
            logger.error(f"Exception while setting ManagedOOMMemoryPressure on {user_service}", exc_info=True)
            return False

    def set_parameter(self, cgroup: str, param: str, value: str) -> bool:
        try:
            path = os.path.join(self.get_full_path(cgroup), param)
            logger.debug(f"mem set_parameter path = {path}")
            write_cgroup_file(value, path)
            return True
        except OSError as e:
            logger.error(f"Failed to set {param}={value}: {e}")
            return False

    def protect(self, cgroup: str, min_bytes: int) -> bool:
        """Set ``memory.min``: memory below this is never reclaimed, by us or the kernel."""
        return self.set_parameter(cgroup, "memory.min", str(min_bytes))
