"""
sandbox.py — Divided Oracle: Isolated execution of untrusted submissions
=========================================================================

A submission is arbitrary Python written by someone you have never met.
This module runs it in a separate process and speaks to it over a pipe, so
that the engine's own memory -- the coin vector, both private hands, the
hidden score S -- is not merely private by convention but absent from the
address space the bot runs in.

THE FIVE THINGS THIS BUYS, AND WHY EACH ONE IS NECESSARY
---------------------------------------------------------

1. The hidden state is unreachable.

   Python frames are introspectable. Running in-process, a forty-line bot
   can do this:

       f = sys._getframe()
       while f:
           if "score" in f.f_locals: return f.f_locals["score"]
           f = f.f_back

   and quote a perfect two-sided market every round. No output validation
   detects it -- every action it returns is legal. Blocking `import sys` in
   the source does not help either, because `__import__("sys")` is a call,
   not an import statement, and an AST import scan never sees it. The only
   real fix is for `score` not to exist in that process. The worker is
   started with "spawn" rather than "fork" precisely for this: a forked
   child would inherit a copy of the parent's entire address space, hidden
   score included.

2. Memory and CPU are capped by the operating system.

   See `limits.py`. On POSIX the worker caps itself with setrlimit, both
   soft and hard. On Windows -- where there is no setrlimit, and where
   every cap therefore used to do nothing at all -- the parent puts the
   worker in a Job Object before releasing it.

3. A timeout can actually fire.

   `signal.alarm` cannot interrupt a tight loop in the same thread, and a
   parent-side `.get(timeout=...)` on a worker pool leaves the worker
   running and its slot occupied forever. A hung bot must be killed, and
   only its parent can kill it. The parent bills CPU, not wall clock, so
   a bot descheduled under `--jobs 16` is not charged for the wait.

4. A submission cannot reach outside the game.

   See `policy.py`: the module cache is purged, the builtins are rewritten
   at the root, and an audit hook -- which CPython offers no way to remove
   -- refuses the operations that touch the OS.

5. Statelessness is enforced rather than requested.

   The rules say a bot may not carry state between deals. Fresh instances
   do not deliver that: module globals and class attributes outlive every
   instance, so a bot can accumulate a profile of each opponent across a
   whole tournament. Here the submission's module is re-executed from
   source before each deal AND the allowed modules are restored to their
   pristine contents, so there is nowhere left to park anything.

WHAT THIS IS NOT
----------------

This is fault isolation and cheat deterrence. It is not a security
boundary. A submission runs as your user, and a CPython escape reaching a
syscall with no audit event remains conceivable. What is guaranteed is
narrower and more useful: nothing a submission does can win it the game,
because the score is in another process, and nothing it allocates or
computes can exhaust the host, because the OS says so. For a public
competition, run the whole tournament inside a container with no network
and a read-only mount.
"""

from __future__ import annotations

import multiprocessing as mp
import sys
import time
from pathlib import Path

import limits
import policy
from engine import BotTimeout, derive_seed
from game_config import GameConfig
from policy import ALLOWED_MODULES, SandboxViolation

__all__ = ["SandboxedBot", "SandboxError", "ALLOWED_MODULES"]


#: Marker swapped in for the GameConfig on every call. A plain string
#: rather than a sentinel instance, so that nothing the parent sends
#: requires the unpickler to resolve a name out of this module.
_CONFIG_MARKER = "\x00__divided_oracle_config__\x00"

#: How often the parent looks at a busy worker's CPU meter. A bot that
#: answers promptly never waits this long -- poll() returns the moment the
#: reply lands -- so this costs nothing except to bots that are overrunning.
_POLL_INTERVAL_S = 0.005


class SandboxError(Exception):
    """The worker died, or never came up."""


# ════════════════════════════════════════════════════════════════════
#  WORKER SIDE
# ════════════════════════════════════════════════════════════════════

class _CallTimeout(BaseException):
    """The in-worker alarm fired: this call overran its own budget.

    BaseException, not Exception: a submission whose hot loop is wrapped in
    `except Exception:` would otherwise swallow its own timeout and carry
    on running, which is exactly the bot the limit exists for.
    """


def _install_call_alarm():
    """Return (arm, disarm) for a per-call timer inside the worker.

    Where it exists, this is the gentle path: the bot's call is abandoned
    but the worker survives, so the deal continues with fallbacks instead
    of a process restart. The timer runs HERE, around the bot's own frame,
    so it measures the bot's execution and not the round trip.

    Returns (None, None) on Windows, which has no setitimer. There the
    parent's CPU monitor is the enforcement instead -- less gentle, since
    it kills, but it also catches the C-level loops that no Python-level
    interrupt can touch on any platform.
    """
    try:
        import signal
    except ImportError:                       # pragma: no cover
        return None, None
    if not hasattr(signal, "setitimer"):      # pragma: no cover - Windows
        return None, None

    def _fire(signum, frame):
        raise _CallTimeout()

    signal.signal(signal.SIGALRM, _fire)

    def arm(seconds: float):
        signal.setitimer(signal.ITIMER_REAL, seconds)

    def disarm():
        signal.setitimer(signal.ITIMER_REAL, 0.0)

    return arm, disarm


# ── return-value normalisation ─────────────────────────────

def _clamped_int(value, limit: int) -> int:
    """Coerce to int within +/- limit, refusing anything absurd.

    Checked by bit_length before conversion: `int(10 ** 1000000)` is a
    perfectly valid Python expression whose decimal conversion alone can
    take longer than the entire time budget.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        if value.bit_length() > 64:
            return limit if value > 0 else -limit
        return max(-limit, min(int(value), limit))
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite number")
        return max(-limit, min(int(value), limit))
    raise TypeError(f"expected a number, got {type(value).__name__}")


def _normalise(method: str, value, limit: int):
    """Reduce a bot's return value to primitives, inside the worker.

    Nothing a submission constructs is ever pickled back to the parent.
    That closes three separate holes at once:

      - code execution in the parent, via an object with a hostile
        __reduce__, since unpickling is not a passive operation
      - memory exhaustion in the parent, via a multi-gigabyte return
      - a crash in the parent, via a return value whose type the engine's
        sanitisers did not anticipate: `quote()` returning a dict used to
        raise KeyError out of `int(result[0])`, which is a *parent-side*
        traceback caused by a *bot-side* mistake

    Anything unrecognisable raises here, and the engine applies that
    method's documented fallback -- the same outcome as a bot that crashed,
    which is what this is.
    """
    if method == "reset":
        return None

    if method == "use_transform":
        return bool(value)

    if method == "bid":
        if not isinstance(value, dict):
            raise TypeError(f"bid() must return a dict, got {type(value).__name__}")
        if len(value) > 64:
            raise ValueError("bid() returned an implausible number of keys")
        out = {}
        for key, amount in value.items():
            if not isinstance(key, str) or len(key) > 64:
                continue
            out[key] = _clamped_int(amount, limit)
        return out

    if method == "quote":
        if isinstance(value, dict) or isinstance(value, (str, bytes)):
            raise TypeError(
                f"quote() must return (bid, ask), got {type(value).__name__}")
        try:
            bid, ask = value
        except (TypeError, ValueError):
            raise TypeError(
                f"quote() must return exactly two numbers, "
                f"got {type(value).__name__}") from None
        return (_clamped_int(bid, limit), _clamped_int(ask, limit))

    if method == "respond":
        if isinstance(value, str):
            # Long strings are truncated rather than rejected so the engine
            # can still warn the author about what they actually returned.
            return value[:32]
        if isinstance(value, dict):
            raise TypeError("respond() must return a string or a tuple, got dict")
        try:
            kind, new_bid, new_ask = value
        except (TypeError, ValueError):
            raise TypeError(
                f"respond() must be 'ACCEPT_BUY', 'ACCEPT_SELL' or "
                f"('COUNTER', bid, ask); got {type(value).__name__}") from None
        if not isinstance(kind, str):
            raise TypeError("respond() action must be a string")
        return (kind[:32], _clamped_int(new_bid, limit), _clamped_int(new_ask, limit))

    raise TypeError(f"unknown method {method!r}")


def _brief(exc: BaseException) -> str:
    """One-line description of an exception, without touching the source.

    `traceback.format_exc()` asks linecache for the offending source line,
    which opens a file -- and after lockdown, opening a file raises, so the
    error handler would fail inside the error handler. The engine only ever
    used the last line of the traceback anyway.
    """
    try:
        return f"{type(exc).__name__}: {exc}"[:400]
    except Exception:                          # pragma: no cover
        return type(exc).__name__


# ── worker main ────────────────────────────────────────────

def _worker_main(conn, source: str, filename: str, overrides: dict,
                 memory_mb: int, cpu_seconds: int, call_limit_s: float) -> None:
    """Entry point for a worker process. Never raises.

    An exception escaping here is not merely untidy: multiprocessing's
    bootstrap handles it by importing `traceback` to print it, and after
    lockdown importing anything raises, so the failure would be replaced by
    a far more confusing one. The parent notices a dead worker on its own.
    """
    try:
        _serve(conn, source, filename, overrides, memory_mb, cpu_seconds,
               call_limit_s)
    except BaseException:
        pass
    finally:
        try:
            conn.close()
        except BaseException:
            pass


def _serve(conn, source: str, filename: str, overrides: dict,
           memory_mb: int, cpu_seconds: int, call_limit_s: float) -> None:
    """Serve calls for one bot until told to shut down.

    Protocol, parent -> worker:
        ("hello",)               ready for the job object to be attached
        ("new_deal", seed)       construct a fresh Bot from source
        ("call", method, args)   invoke a method on it
        ("stop",)                exit

    Worker -> parent:
        ("ready", applied_limits, 0.0)
        ("ok", value, elapsed_ms)
        ("err", text, elapsed_ms)
        ("timeout", text, elapsed_ms)
        ("tamper", names, 0.0)   carried alongside the next reply
    """
    applied = limits.apply_self_limits(memory_mb, cpu_seconds)
    sys.dont_write_bytecode = True
    arm, disarm = _install_call_alarm()

    # Everything the worker will ever need must be imported and referenced
    # before lockdown, because afterwards it cannot import anything.
    config = GameConfig(**overrides)
    max_value = config.MAX_REASONABLE_VALUE
    bot = None

    try:
        code = compile(source, filename, "exec")
    except BaseException as exc:
        # A file that will not even compile still has to answer the parent,
        # or the parent waits out its whole deadline for nothing.
        compile_error = _brief(exc)
        code = None
    else:
        compile_error = None

    # Tell the parent we exist, and block until it has jailed us. No
    # submission code has run at this point, and none will until the job
    # object is attached -- otherwise there is a window in which a bot can
    # allocate freely.
    conn.send(("ready", tuple(applied), 0.0))
    try:
        if conn.recv()[0] != "go":
            return
    except (EOFError, KeyboardInterrupt):
        return

    # Snapshot after the allowlist is warm -- a module that is not loaded
    # yet has no globals to restore later -- and before anything from the
    # submission has had a chance to touch it. The namedtuple pin goes
    # first of all, so the snapshot records it and the per-deal restore
    # keeps it instead of flagging it as tampering.
    policy.preimport_allowed()
    policy.pin_namedtuple_module()
    # Guards everything importable, not just the participant-facing list:
    # a module a bot can import is a module it can park state in between
    # deals, and that is true of `__future__` as much as of `functools`.
    state = policy.StateGuard(policy.IMPORTABLE_MODULES)
    policy.lock_down([code] if code is not None else [],
                     limits_applied=applied)

    while True:
        try:
            msg = conn.recv()
        except (EOFError, KeyboardInterrupt):
            return

        kind = msg[0]
        if kind == "stop":
            return

        if kind == "new_deal":
            start = time.perf_counter()
            deal_seed = msg[1] if len(msg) > 1 else 0
            # Undo anything the last deal left behind BEFORE building the
            # next bot, so a submission cannot read its own leftovers.
            tampered = state.restore(deal_seed)
            try:
                if code is None:
                    raise RuntimeError(compile_error or "submission did not compile")
                namespace = {"__name__": "__submission__", "__file__": filename}
                exec(code, namespace)          # noqa: S102 - the whole point
                bot_cls = namespace.get("Bot")
                if bot_cls is None:
                    raise AttributeError(
                        "submission does not define a class named 'Bot'")
                bot = bot_cls()
                conn.send(("ok", tuple(tampered),
                           (time.perf_counter() - start) * 1000))
            except BaseException as exc:
                bot = None
                conn.send(("err", _brief(exc), (time.perf_counter() - start) * 1000))
            continue

        if kind == "call":
            _, method, args = msg
            if bot is None:
                conn.send(("err", "no bot instance for this deal", 0.0))
                continue
            # The config is passed by marker and never pickled per call:
            # sending the whole rule set on every call would dominate IPC.
            args = tuple(
                config if isinstance(a, str) and a == _CONFIG_MARKER else a
                for a in args
            )
            start = time.perf_counter()
            try:
                fn = getattr(bot, method, None)
                if fn is None:
                    raise AttributeError(f"{method}() is not implemented")
                if arm is not None:
                    arm(call_limit_s)
                try:
                    raw = fn(*args)
                finally:
                    if disarm is not None:
                        disarm()
                value = _normalise(method, raw, max_value)
                conn.send(("ok", value, (time.perf_counter() - start) * 1000))
            except _CallTimeout:
                conn.send((
                    "timeout",
                    f"{method}() ran longer than {call_limit_s * 1000:.0f}ms",
                    (time.perf_counter() - start) * 1000,
                ))
            except SandboxViolation as exc:
                conn.send(("err", f"policy violation -- {exc}",
                           (time.perf_counter() - start) * 1000))
            except BaseException as exc:
                conn.send(("err", _brief(exc), (time.perf_counter() - start) * 1000))
            continue

        conn.send(("err", f"unknown command {kind!r}", 0.0))


# ════════════════════════════════════════════════════════════════════
#  PARENT SIDE
# ════════════════════════════════════════════════════════════════════

class SandboxedBot:
    """A bot running in its own process, quacking like a local bot object.

    Exposes reset/bid/quote/respond/use_transform so that
    :class:`engine.BotWrapper` cannot tell the difference, plus
    ``last_call_ms`` so the engine charges the bot for its own compute and
    not for this module's IPC.

    One worker serves a whole pairing. ``new_deal()`` re-executes the
    submission between deals, and doubles as the per-deal factory the
    engine's ``play_match`` expects. With ``fresh_process_per_deal`` the
    worker is replaced outright instead, which is slower but leaves nothing
    whatsoever behind -- the setting to use when a result is being appealed.
    """

    def __init__(self, path: str | Path, name: str, config: GameConfig,
                 fresh_process_per_deal: bool = False):
        self.path = Path(path)
        self.name = name
        self.config = config
        self.source = self.path.read_text(encoding="utf-8")
        self.last_call_ms: float | None = None
        self.restarts = 0
        self.tampering: list[str] = []
        self.limit_warnings: list[str] = []
        #: Why the submission could not be loaded for a deal, if it could not.
        self.load_errors: list[str] = []
        self.fresh_process_per_deal = fresh_process_per_deal
        self._deal_index = 0
        # Set when the worker had to be killed. A replacement worker has no
        # bot instance and no per-deal state, so the bot is out for the rest
        # of this deal; new_deal() clears it.
        self._dead_this_deal = False
        self._ctx = mp.get_context("spawn")
        self._proc = None
        self._conn = None
        self._jail = limits.ExternalLimits()
        # A worker already starting up, ready to take over at the next deal.
        self._spare = None
        # The bot's own budget, enforced by an alarm inside the worker where
        # one exists and by the CPU monitor below everywhere.
        self._call_limit_s = config.HARD_TIME_LIMIT_MS / 1000.0
        # The CPU a call may burn before the parent stops waiting and kills.
        # Deliberately looser than the per-call limit the bot is *scored*
        # against: CPU meters are coarse (Windows updates in ~15ms steps),
        # and a bot at 60ms should be charged a violation by the precise
        # in-worker clock, not executed by a rounding error.
        self._cpu_kill_s = max(2.0 * self._call_limit_s,
                               self._call_limit_s + 0.05)
        # A pure liveness backstop for when the CPU meter is unreadable or
        # the worker is gone: absorbs IPC and OS scheduling, neither of
        # which is the bot's fault.
        self._deadline_s = max(2.0, 20 * self._call_limit_s)
        self._start()

    # ── lifecycle ──────────────────────────────────────────

    def _spawn(self):
        """Launch a worker and jail it, without waiting for it to come up.

        Returns (proc, conn, jail). The child spends about 40ms starting an
        interpreter and importing; `Process.start()` does not wait for any
        of that, so the caller can do something useful in the meantime and
        collect the worker later with :meth:`_activate`.

        The job object is attached here rather than after the handshake
        because it can be, and the earlier the better -- the guarantee that
        matters is only that it precedes the "go", which releases the worker
        into submission code.
        """
        parent_conn, child_conn = self._ctx.Pipe()
        cpu_seconds = max(1, int(self.config.MATCH_TIMEOUT_S))
        proc = self._ctx.Process(
            target=_worker_main,
            args=(child_conn, self.source, str(self.path),
                  dict(self.config._overrides),
                  self.config.BOT_MEMORY_LIMIT_MB, cpu_seconds,
                  self._call_limit_s),
            daemon=True,
        )
        proc.start()
        child_conn.close()
        jail = limits.attach_external_limits(
            proc.pid, self.config.BOT_MEMORY_LIMIT_MB, cpu_seconds)
        return proc, parent_conn, jail

    def _activate(self, spawned) -> None:
        """Wait for a spawned worker's handshake and let it run."""
        proc, conn, jail = spawned
        self._proc, self._conn, self._jail = proc, conn, jail

        applied: tuple = ()
        try:
            if conn.poll(30.0):
                reply = conn.recv()
                if reply[0] == "ready":
                    applied = reply[1]
        except (EOFError, OSError):
            pass

        if not applied and not jail and not self.limit_warnings:
            # Said out loud, once per bot, rather than running uncapped in
            # silence -- which is what the old code did on every Windows box,
            # and what a later bug in limits.py did again even after the caps
            # were written. An unenforced limit that nobody mentions is worse
            # than no limit, because the standings look trustworthy.
            self.limit_warnings.append(
                f"{self.name}: NO OS resource caps are in force "
                f"({jail.reason or 'no mechanism available'}); memory and "
                f"CPU are unbounded for this worker. Run the tournament inside "
                f"a container with no network and a read-only mount."
            )
        try:
            conn.send(("go",))
        except (BrokenPipeError, OSError):
            pass

    def _start(self) -> None:
        """Bring up a worker, using the pre-warmed spare if there is one."""
        spare, self._spare = self._spare, None
        self._activate(spare if spare is not None else self._spawn())

    def _prewarm(self) -> None:
        """Start the next deal's worker now, so its startup overlaps this one.

        Only used with fresh_process_per_deal, where the alternative is
        paying the full interpreter startup at the top of every deal with
        nothing else happening.
        """
        if self._spare is None and self.fresh_process_per_deal:
            try:
                self._spare = self._spawn()
            except Exception:
                self._spare = None

    @staticmethod
    def _dispose(proc, conn, jail) -> None:
        """Kill one worker and release its handles. Never raises."""
        for action in (
            lambda: proc.kill(),
            lambda: proc.join(timeout=2.0),
            lambda: conn.close(),
            # Closing the job kills anything the worker left running, and
            # would have killed the worker itself had the kill above failed.
            lambda: jail.close(),
        ):
            try:
                action()
            except Exception:
                pass

    def _kill(self) -> None:
        if self._proc is None:
            return
        self._dispose(self._proc, self._conn, self._jail)
        self._jail = limits.ExternalLimits()
        self._proc, self._conn = None, None

    def _restart(self) -> None:
        self._kill()
        self.restarts += 1
        self._dead_this_deal = True
        self._start()

    def close(self) -> None:
        """Shut the worker down cleanly; kill it if it will not go."""
        if self._spare is not None:
            spare, self._spare = self._spare, None
            self._dispose(*spare)

        if self._proc is None:
            self._jail.close()
            return
        try:
            self._conn.send(("stop",))
            self._proc.join(timeout=1.0)
        except Exception:
            pass
        if self._proc.is_alive():
            self._kill()
        else:
            try:
                self._conn.close()
            except Exception:
                pass
            try:
                self._jail.close()
            except Exception:
                pass
            self._jail = limits.ExternalLimits()
            self._proc, self._conn = None, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── rpc ────────────────────────────────────────────────

    def _await_reply(self, timeout_s: float) -> bool:
        """Wait for the worker, killing it if it burns too much CPU.

        Returns True when a reply is waiting. Raises BotTimeout when the
        worker had to be killed.

        The bot is billed for CPU, not for elapsed time. A parent-side
        stopwatch also times OS scheduling delay, and with pairings running
        across every core that kills entries whose real cost is
        microseconds -- it happened on the first run of this tournament.
        A descheduled worker simply accumulates no CPU.
        """
        pid = self._proc.pid if self._proc is not None else None
        started = time.perf_counter()
        cpu0 = limits.process_cpu_seconds(pid) if pid else None

        while True:
            if self._conn.poll(_POLL_INTERVAL_S):
                return True

            elapsed = time.perf_counter() - started
            if cpu0 is not None:
                cpu_now = limits.process_cpu_seconds(pid)
                if cpu_now is not None and cpu_now - cpu0 > self._cpu_kill_s:
                    self._restart()
                    self.last_call_ms = (cpu_now - cpu0) * 1000
                    raise BotTimeout(
                        f"{self.name}: burned {(cpu_now - cpu0) * 1000:.0f}ms of "
                        f"CPU on one call; worker killed and restarted"
                    )
            if elapsed > timeout_s:
                self._restart()
                self.last_call_ms = timeout_s * 1000
                raise BotTimeout(
                    f"{self.name}: unresponsive for {timeout_s:.1f}s; "
                    f"worker killed and restarted"
                )

    def _rpc(self, message, timeout_s: float):
        """Send one command and wait for its reply.

        A worker that does not answer is killed and replaced, and the
        caller gets BotTimeout. Killing is what makes the limit real: the
        bot's frame is still running, and nothing short of the process
        dying will stop it.
        """
        if self._proc is None:
            self._start()
        try:
            self._conn.send(message)
        except (BrokenPipeError, OSError) as e:
            self._restart()
            raise SandboxError(f"{self.name}: worker pipe broke ({e})") from None

        self._await_reply(timeout_s)

        try:
            status, value, elapsed = self._conn.recv()
        except (EOFError, OSError):
            # Died mid-call: the memory cap, the CPU cap, or a segfault.
            self._restart()
            self.last_call_ms = None
            raise SandboxError(f"{self.name}: worker died during the call") from None

        self.last_call_ms = elapsed
        if status == "timeout":
            # The worker caught it itself, so the process is still healthy
            # and holding this deal's state -- no kill and no restart, though
            # the engine still ends the bot's deal.
            raise BotTimeout(f"{self.name}: {value}")
        if status == "err":
            raise RuntimeError(value or "bot error")
        return value

    # ── engine-facing API ──────────────────────────────────

    def new_deal(self) -> "SandboxedBot":
        """Re-execute the submission and build a fresh Bot for the next deal.

        Returns self, so this doubles as the per-deal factory play_match
        expects. Failures are deliberately swallowed: a submission that
        cannot even be constructed is handled as a bot whose reset() failed,
        which the engine already knows how to score.
        """
        self.last_call_ms = None
        self._dead_this_deal = False
        self._deal_index += 1

        if self.fresh_process_per_deal and self._deal_index > 1:
            # Nothing carries over from a process that no longer exists.
            # Buys a guarantee rather than a clean-up; most of the cost is
            # hidden because the replacement has been starting up since the
            # last deal began.
            self._kill()
            self._start()
        self._prewarm()

        # Derived rather than the raw deal index so that the global RNG a
        # bot might lean on is reproducible across reruns without being the
        # same stream in every pairing.
        state_seed = derive_seed("deal-state", self.path.name, self._deal_index)
        try:
            tampered = self._rpc(("new_deal", state_seed),
                                 timeout_s=max(10.0, self._deadline_s))
            for name in (tampered or ()):
                self.tampering.append(name)
        except (BotTimeout, SandboxError, RuntimeError) as e:
            # Kept, not swallowed. Everything that makes a submission
            # unloadable arrives here -- an illegal import, a policy
            # violation, a module-level crash -- and discarding it left the
            # organiser looking at "no bot instance for this deal" repeated
            # once per call, with nothing to tell the author. Recorded once
            # per distinct reason so a bad entry does not fill the log.
            reason = f"{type(e).__name__}: {e}"
            if reason not in self.load_errors:
                self.load_errors.append(reason)
        return self

    def _call_remote(self, method: str, *args):
        if self._dead_this_deal:
            # Do not pay another IPC round trip to a worker we already know
            # has no instance: the engine treats this as the same forfeited
            # action it would get from a fresh timeout.
            self.last_call_ms = None
            raise BotTimeout(f"{self.name}: out for this deal after a timeout")
        return self._rpc(("call", method, args), self._deadline_s)

    def reset(self, seat, config, seed):
        # The worker holds its own config; sending it per call would pickle
        # the whole rule set on every deal for no reason.
        return self._call_remote("reset", seat, _CONFIG_MARKER, seed)

    def bid(self, obs, offered):
        return self._call_remote("bid", obs, offered)

    def quote(self, obs):
        return self._call_remote("quote", obs)

    def respond(self, obs, quote, turn):
        return self._call_remote("respond", obs, quote, turn)

    def use_transform(self, obs):
        return self._call_remote("use_transform", obs)
