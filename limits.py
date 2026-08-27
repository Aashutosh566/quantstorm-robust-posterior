"""
limits.py — Divided Oracle: OS-enforced resource caps for bot workers
======================================================================

Memory and CPU are capped by the operating system, not by Python, because
a submission that has already exhausted memory cannot be relied upon to
run the code that would have stopped it.

WHY THIS MODULE EXISTS SEPARATELY FROM sandbox.py
--------------------------------------------------

The two platforms enforce from opposite ends:

  POSIX    the worker caps itself, first thing, with setrlimit(). Both the
           soft AND hard limits are lowered, so a submission that somehow
           reaches `resource` cannot raise them back -- lowering only the
           soft limit, as this code used to, leaves the cap advisory to
           anyone who can call setrlimit themselves.

  Windows  there is no setrlimit. The equivalent is a Job Object, which is
           created and applied by the PARENT and cannot be detached by the
           child. `_apply_limits` used to `import resource`, fail, and
           return, which meant every cap silently did nothing on Windows:
           no memory limit, no CPU limit, no bar on spawning processes.

Because the Windows cap is applied from outside, there is a window between
`proc.start()` and `AssignProcessToJobObject` in which the child is
unlimited. `sandbox.py` closes it with a handshake: the worker sends
"ready" and blocks until the parent, having attached the job, answers
"go". No submission code runs before then.

WHAT EACH CAP BUYS
------------------

  memory      an allocation past the cap raises MemoryError in the bot's
              own frame, which the engine already scores as a crashed call
  cpu         a runaway that the per-call timer cannot interrupt (a C-level
              loop) is killed by the OS
  processes   the worker cannot fork or spawn, so it cannot escape its own
              limits by delegating to a child
  files       the worker cannot write anything, anywhere (POSIX only; on
              Windows this is `open` being absent from builtins plus the
              audit hook in policy.py)

None of this is a security boundary on its own -- see the threat model in
sandbox.py, and prefer a container for a public event.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

__all__ = [
    "apply_self_limits",
    "attach_external_limits",
    "process_cpu_seconds",
    "ExternalLimits",
    "HAVE_SETRLIMIT",
    "HAVE_JOB_OBJECTS",
]

IS_WINDOWS = sys.platform == "win32"

try:                                  # POSIX
    import resource as _resource
    HAVE_SETRLIMIT = True
except ImportError:                   # pragma: no cover - Windows
    _resource = None
    HAVE_SETRLIMIT = False


# ════════════════════════════════════════════════════════════════════
#  POSIX — the worker caps itself
# ════════════════════════════════════════════════════════════════════

def apply_self_limits(memory_mb: int, cpu_seconds: int) -> list[str]:
    """Cap this process from the inside. Returns the names actually applied.

    Called first thing in the worker, before any submission code exists in
    the address space. On Windows this is a no-op and the caps arrive from
    the parent instead -- see :func:`attach_external_limits`.

    Both soft and hard limits are lowered. Once the hard limit is down it
    cannot be raised again by an unprivileged process, so this survives a
    submission that gets hold of `resource` through some route the import
    policy missed.
    """
    if not HAVE_SETRLIMIT:            # pragma: no cover - Windows
        return []

    applied: list[str] = []
    for name, limit in (
        ("RLIMIT_AS", memory_mb * 1024 * 1024),   # address space
        ("RLIMIT_DATA", memory_mb * 1024 * 1024),  # heap, belt and braces
        ("RLIMIT_CPU", cpu_seconds),              # cpu seconds, then SIGXCPU
        ("RLIMIT_FSIZE", 0),                      # cannot write any file
        ("RLIMIT_NPROC", 0),                      # cannot fork
        ("RLIMIT_CORE", 0),                       # no core dumps
    ):
        rlimit = getattr(_resource, name, None)
        if rlimit is None:
            continue
        try:
            _soft, hard = _resource.getrlimit(rlimit)
            if hard in (_resource.RLIM_INFINITY, -1):
                target = limit
            else:
                target = min(limit, hard)
            # Lower the hard limit too: an advisory cap is not a cap.
            _resource.setrlimit(rlimit, (target, target))
            applied.append(name)
        except (ValueError, OSError):
            # A limit the platform declines to set is reported by omission
            # rather than swallowed, so the caller can log the shortfall.
            pass
    return applied


# ════════════════════════════════════════════════════════════════════
#  WINDOWS — the parent caps the child with a Job Object
# ════════════════════════════════════════════════════════════════════

#: Windows plumbing is built on first use, never at import time.
#:
#: This matters more than it looks. ``ctypes`` can call any function in any
#: DLL, so a worker with ctypes loaded hands a submission a target worth
#: chasing: ``object.__subclasses__()`` reaches a ctypes type, its methods'
#: ``__globals__`` reach the ctypes module, and from there ``CDLL`` is a
#: normal Python name. The worker therefore never imports ctypes at all --
#: only the parent does, and only to build the Job Object. Everything below
#: runs parent-side.
_WIN = None


def _win():                           # pragma: no cover - POSIX
    """Build and cache the kernel32 bindings. Parent-side only."""
    global _WIN
    if _WIN is not None:
        return _WIN

    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetProcessTimes.restype = wintypes.BOOL
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
        ctypes.POINTER(wintypes.FILETIME)] * 4

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _JOB_ACCESS = (_PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_SET_QUOTA
                   | _PROCESS_TERMINATE)

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXTENDED(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    # SimpleNamespace rather than a class body on purpose: inside a
    # function, `class X: ctypes = ctypes` does not mean what it looks like.
    # A class body resolves names with LOAD_NAME, which consults the class
    # namespace, then globals, then builtins -- and skips the enclosing
    # function's locals entirely. It raises NameError, `attach_external_limits`
    # catches it, and every cap silently becomes a no-op. That is exactly the
    # bug this module exists to fix, so it is worth a comment.
    _WIN = SimpleNamespace(
        ctypes=ctypes,
        wintypes=wintypes,
        k32=_k32,
        EXTENDED=_EXTENDED,
        QUERY=0x1000,                        # PROCESS_QUERY_LIMITED_INFORMATION
        JOB_ACCESS=0x1000 | 0x0100 | 0x0001,  # + SET_QUOTA + TERMINATE
        LIMIT_ACTIVE_PROCESS=0x00000008,
        LIMIT_PROCESS_TIME=0x00000002,
        LIMIT_PROCESS_MEMORY=0x00000100,
        LIMIT_DIE_ON_UNHANDLED_EXCEPTION=0x00000400,
        LIMIT_KILL_ON_JOB_CLOSE=0x00002000,
        ExtendedLimitInformation=9,
    )
    return _WIN


#: True where an external, unremovable cap can be applied to a child.
HAVE_JOB_OBJECTS = IS_WINDOWS


class ExternalLimits:
    """A Job Object holding one worker, or an inert placeholder elsewhere.

    Kept alive by the caller for as long as the worker should live. Closing
    it kills the worker, because the job carries KILL_ON_JOB_CLOSE -- which
    also means a parent that crashes outright does not leak bot processes,
    since the handle dies with it.
    """

    __slots__ = ("_job", "applied", "reason")

    def __init__(self, job=None, applied: tuple[str, ...] = (), reason: str = ""):
        self._job = job
        self.applied = applied
        #: Why nothing was applied, when nothing was. Carried rather than
        #: swallowed: the first version of this module returned an inert
        #: object on every failure path with no explanation, so a NameError
        #: in the setup code turned every cap into a no-op and said nothing.
        self.reason = reason

    def __bool__(self) -> bool:
        return self._job is not None

    def close(self) -> None:
        if self._job is None:
            return
        job, self._job = self._job, None
        if IS_WINDOWS:                # pragma: no cover - POSIX
            try:
                _win().k32.CloseHandle(job)
            except Exception:
                pass


def attach_external_limits(pid: int, memory_mb: int,
                           cpu_seconds: int) -> ExternalLimits:
    """Cap another process from outside. Windows only; inert on POSIX.

    Must be called before the target runs any submission code -- sandbox.py
    holds the worker at a handshake until this has returned.

    Failure is reported by an ExternalLimits that is falsy, never by an
    exception: a tournament should not die because one worker could not be
    jailed, but the caller does need to know so it can warn.
    """
    if not IS_WINDOWS:
        return ExternalLimits(reason="not Windows; setrlimit applies instead")

    try:                              # pragma: no cover - POSIX
        w = _win()
    except Exception as exc:
        return ExternalLimits(reason=f"kernel32 bindings unavailable: {exc!r}")

    k32, ctypes = w.k32, w.ctypes
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return ExternalLimits(reason="CreateJobObjectW failed")

    info = w.EXTENDED()
    info.BasicLimitInformation.LimitFlags = (
        w.LIMIT_PROCESS_MEMORY               # hard commit cap -> MemoryError
        | w.LIMIT_ACTIVE_PROCESS             # no child processes
        | w.LIMIT_PROCESS_TIME               # cpu ceiling
        | w.LIMIT_KILL_ON_JOB_CLOSE          # cleanup even if the parent dies
        | w.LIMIT_DIE_ON_UNHANDLED_EXCEPTION  # no crash dialog to hang on
    )
    info.BasicLimitInformation.ActiveProcessLimit = 1
    # PerProcessUserTimeLimit counts in 100-nanosecond units.
    info.BasicLimitInformation.PerProcessUserTimeLimit = int(
        max(1, cpu_seconds) * 10_000_000)
    info.ProcessMemoryLimit = int(memory_mb) * 1024 * 1024

    if not k32.SetInformationJobObject(
            job, w.ExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info)):
        k32.CloseHandle(job)
        return ExternalLimits(
            reason=f"SetInformationJobObject failed "
                   f"(error {ctypes.get_last_error()})")

    handle = k32.OpenProcess(w.JOB_ACCESS, False, int(pid))
    if not handle:
        k32.CloseHandle(job)
        return ExternalLimits(
            reason=f"OpenProcess({pid}) failed "
                   f"(error {ctypes.get_last_error()})")
    try:
        assigned = k32.AssignProcessToJobObject(job, handle)
        error = ctypes.get_last_error()
    finally:
        k32.CloseHandle(handle)

    if not assigned:
        k32.CloseHandle(job)
        return ExternalLimits(
            reason=f"AssignProcessToJobObject failed (error {error})")

    return ExternalLimits(job, ("memory", "cpu", "processes"))


# ════════════════════════════════════════════════════════════════════
#  CPU ACCOUNTING  (for the parent-side timing monitor)
# ════════════════════════════════════════════════════════════════════

def process_cpu_seconds(pid: int) -> float | None:
    """CPU seconds consumed by another process, or None if unavailable.

    The parent's timing monitor bills a bot for CPU rather than wall clock
    on purpose. A wall-clock stopwatch in the parent also times OS
    scheduling delay, and under `--jobs 16` on a busy machine that kills
    innocent entries whose real cost is microseconds. Charging CPU means a
    descheduled bot is simply not accumulating any.

    Returns None on platforms where per-process CPU cannot be read, and the
    caller falls back to its generous liveness deadline.
    """
    if IS_WINDOWS:                    # pragma: no cover - POSIX
        try:
            w = _win()
        except Exception:
            # attach_external_limits reports the same failure with its
            # reason attached, so this path stays quiet rather than warning
            # once per poll.
            return None
        k32, ctypes, wintypes = w.k32, w.ctypes, w.wintypes
        handle = k32.OpenProcess(w.QUERY, False, int(pid))
        if not handle:
            return None
        creation, exited = wintypes.FILETIME(), wintypes.FILETIME()
        kernel, user = wintypes.FILETIME(), wintypes.FILETIME()
        try:
            ok = k32.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user))
        finally:
            k32.CloseHandle(handle)
        if not ok:
            return None

        def _seconds(ft) -> float:
            return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7

        return _seconds(kernel) + _seconds(user)

    # Linux: utime and stime are fields 14 and 15 of /proc/<pid>/stat, but
    # the command name in field 2 may itself contain spaces and brackets,
    # so split after the last ')' rather than naively on whitespace.
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as fh:
            data = fh.read()
        fields = data[data.rindex(b")") + 2:].split()
        ticks = os.sysconf("SC_CLK_TCK")
        return (int(fields[11]) + int(fields[12])) / ticks
    except (OSError, ValueError, IndexError, AttributeError):
        # macOS and anything else without /proc.
        return None
