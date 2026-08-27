"""
policy.py — Divided Oracle: what a submission is allowed to reach
==================================================================

The import allowlist and the capability policy that the worker process
installs around a submission, before a line of it runs.

WHY A NAME-BASED SCAN CANNOT DO THIS JOB
-----------------------------------------

`bot_loader.py` refuses `import os` and `().__class__` in the source. It
is worth having -- it gives the author a clear error -- but it is not the
enforcement, and it cannot be, because a gadget chain runs entirely
through names no scanner would object to. Every submission legitimately
holds an `Obs`, and from it:

    type(obs)                  -> the Obs class
      .__module__ / globals    -> game_config's namespace
      dataclass.__globals__    -> the dataclasses module
      dataclasses.sys          -> the real sys module
      sys.modules["os"]        -> everything

No blocklist of attribute names survives `getattr(x, "__cl" + "ass__")`,
and no import scan sees `__import__("os")` because that is a call.

WHAT DOES WORK: BREAK THE CHAIN, THEN BLOCK THE ACTIONS
--------------------------------------------------------

Two mechanisms, applied in order, neither of which depends on predicting
how the submission spells anything:

1. Purge `sys.modules`. Anything dangerous is removed from the cache, so
   the chain above ends at a KeyError. This has to happen because the
   `import` audit event does NOT fire for a module that is already
   cached -- an audit hook alone would wave `import os` straight through
   on any interpreter that had already imported it, which every worker
   has.

2. Install an audit hook. Audit hooks fire inside CPython, below the
   Python object graph, so they catch an action however it was reached.
   Crucially there is no `sys.removeaudithook`: once installed, a hook
   cannot be uninstalled, by us or by a submission.

After the purge, EVERY import the bot is allowed to make is already
cached, so the hook can deny the `import` event unconditionally. That is
a much stronger rule than matching module names, and it cannot be fooled
by spelling.

The residual, stated plainly: this stops a submission from *doing*
anything outside the game. It is still the same OS user, and a CPython
escape to a syscall with no audit event is conceivable. Memory and CPU
are capped by the OS (see limits.py) and the hidden score is in another
process entirely, so no escape converts into a competitive advantage.
For a public event, run the whole tournament inside a container with no
network and a read-only mount.
"""

from __future__ import annotations

import keyword
import sys

__all__ = [
    "ALLOWED_MODULES",
    "IMPORTABLE_MODULES",
    "SandboxViolation",
    "lock_down",
    "restrict_builtins",
    "purge_modules",
    "pin_namedtuple_module",
    "SUBMISSION_MODULE",
    "LockdownReport",
]


#: Standard-library modules a submission may import. The single source of
#: truth; bot_loader imports it rather than restating it, so the static
#: gate and the runtime cannot drift apart.
ALLOWED_MODULES = frozenset({
    "math", "random", "statistics", "collections", "heapq",
    "bisect", "itertools", "functools", "typing",
})

#: Machinery the language reaches for on a submission's behalf. Importable,
#: but kept out of the participant-facing allowlist above because nobody
#: chooses to depend on these -- they arrive with ordinary syntax.
#:
#: `__future__`: `from __future__ import annotations` is a compiler
#: directive that still emits a real IMPORT_NAME, so both the static gate
#: and the runtime guard saw it and refused. Every file in this repository
#: opens with that line, which made the house style an automatic rejection.
#:
#: `annotationlib`: on 3.14 (PEP 649/749) subclassing `typing.NamedTuple`
#: imports it lazily, so the rulebook's promise that typing.NamedTuple
#: works was false on the interpreter the event actually runs on -- and it
#: failed at *runtime*, after the gate had already admitted the entry.
_LANGUAGE_MODULES = frozenset({"__future__", "annotationlib"})

#: What the static gate and the runtime guard both accept. Wider than
#: ALLOWED_MODULES, which stays the list quoted to participants.
IMPORTABLE_MODULES = ALLOWED_MODULES | _LANGUAGE_MODULES


class SandboxViolation(BaseException):
    """A submission attempted something the policy forbids.

    Derives from BaseException, not Exception, so that a submission
    wrapping its own hot loop in `except Exception:` cannot swallow the
    refusal and carry on.
    """


# ════════════════════════════════════════════════════════════════════
#  MODULE CACHE PURGE
# ════════════════════════════════════════════════════════════════════

#: Roots the worker itself still needs after lockdown. Anything here stays
#: importable, so keep it to what is genuinely required: every entry is a
#: module a submission could also reach, and the audit hook is what makes
#: that harmless rather than the absence of the module.
_WORKER_ESSENTIAL = frozenset({
    "builtins", "sys", "time", "types", "operator", "abc", "_abc",
    "codecs", "_codecs", "encodings", "io", "_io", "enum", "re",
    "keyword", "reprlib", "copyreg", "warnings", "weakref", "_weakref",
    "_collections", "_collections_abc", "_functools", "_heapq", "_bisect",
    "_random", "_statistics", "_operator", "_sre", "sre_compile",
    "sre_constants", "sre_parse", "contextlib", "dataclasses", "numbers",
    "_typing", "traceback", "linecache",
    "multiprocessing", "_multiprocessing", "pickle", "_pickle", "struct",
    "_struct", "array", "queue", "_queue", "select", "selectors", "errno",
    "atexit", "signal", "math", "itertools", "_datetime",
    # Kept because the unpickler rebuilds an Obs by name when one arrives
    # over the pipe. A submission is handed a GameConfig and an Obs anyway,
    # so this discloses nothing it did not already have.
    #
    # Deliberately NOT kept: sandbox, policy, limits and engine. Their live
    # references keep working after a purge, but dropping them from the
    # cache means `sys.modules` no longer leads to the worker's own
    # machinery -- the audit hook's closure, the pipe, or the job handle.
    "game_config",
})

#: Purged even if something above would otherwise have kept them. These are
#: the modules that turn a Python bug into a host compromise, and none has
#: any use inside a trading bot.
_ALWAYS_PURGE = frozenset({
    "os", "posix", "nt", "subprocess", "socket", "_socket", "ssl", "_ssl",
    "ctypes", "_ctypes", "importlib", "imp", "runpy", "pty", "tty",
    "shutil", "tempfile", "glob", "fnmatch", "pathlib", "posixpath",
    "ntpath", "genericpath", "stat", "_stat", "fcntl", "termios", "syslog",
    "resource", "grp", "pwd", "spwd", "crypt", "msvcrt", "winreg",
    "_winapi", "mmap", "marshal", "gc", "inspect", "dis", "ast", "code",
    "codeop", "compileall", "py_compile", "pdb", "bdb", "cmd", "trace",
    "tracemalloc", "faulthandler", "sysconfig", "site", "webbrowser",
    "urllib", "http", "ftplib", "smtplib", "imaplib", "poplib", "telnetlib",
    "nntplib", "xmlrpc", "sqlite3", "dbm", "shelve", "pickletools",
    "platform", "getpass", "uuid", "shlex",
    # A background thread outlives the call that started it, which would
    # both dodge the per-call clock and carry state from one deal into the
    # next. The worker starts its own watchdog before lockdown and holds a
    # direct reference, so purging these does not disarm it.
    "threading", "_thread",
})


def purge_modules(keep_roots: frozenset[str]) -> list[str]:
    """Drop every cached module outside `keep_roots`. Returns what went.

    Deleting from ``sys.modules`` does not unload anything -- code holding
    a live reference keeps working, which is why the worker's own
    machinery survives this. What it does is guarantee that any *future*
    import goes through the import system again, and therefore past the
    audit hook.
    """
    purged: list[str] = []
    for name in list(sys.modules):
        root = name.split(".")[0]
        if name in _ALWAYS_PURGE or root in _ALWAYS_PURGE:
            purged.append(name)
            del sys.modules[name]
            continue
        if root in keep_roots:
            continue
        purged.append(name)
        del sys.modules[name]
    return purged


# ════════════════════════════════════════════════════════════════════
#  BUILTINS
# ════════════════════════════════════════════════════════════════════

#: Modules a pickle arriving over the pipe may resolve names from. The only
#: pickles the worker unpickles come from its own parent, but restricting
#: this costs nothing and removes a class of surprise. `sandbox` is absent
#: on purpose: the config marker crossing the pipe is a plain string, so
#: nothing the parent sends needs to name the sandbox module.
_PICKLE_MODULES = frozenset({
    "builtins", "game_config", "collections", "copyreg",
})

#: Removed from the submission's namespace outright.
#:
#: Shorter than the list bot_loader rejects statically, and deliberately
#: so. `vars`, `globals` and `memoryview` are all on the static list --
#: they have no place in a submission -- but the standard library and
#: multiprocessing's own pipe use them, and a builtin deleted here is
#: deleted for the worker's machinery too. They gain a submission nothing
#: on their own: `vars(x)` is `x.__dict__`, and every module worth reaching
#: through it is either purged or restored between deals.
_BANNED_BUILTINS = frozenset({
    "open", "input", "breakpoint", "help", "exit", "quit",
})

#: Names `__import__` may resolve. The nine are the rules; the rest is what
#: the unpickler needs to rebuild an Obs arriving over the pipe, and every
#: one of them is an object the submission has been handed anyway.
_IMPORTABLE = IMPORTABLE_MODULES | _PICKLE_MODULES


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    """The only `__import__` in the process once lockdown has run."""
    if level != 0:
        raise SandboxViolation("relative imports are not allowed in a submission")
    root = name.split(".")[0]
    if root not in _IMPORTABLE:
        raise SandboxViolation(
            f"import of {name!r} is not allowed. Permitted modules: "
            + ", ".join(sorted(ALLOWED_MODULES))
        )
    return _REAL_IMPORT(name, globals, locals, fromlist, level)


_REAL_IMPORT = __import__


def restrict_builtins() -> dict:
    """Rewrite the dangerous builtins at the root, not in a copy.

    The previous version built a filtered *copy* of ``vars(builtins)`` and
    handed that to the submission as ``__builtins__``. That leaves the real
    module untouched, and every function in the copy still carries a
    reference back to it:

        print.__self__                  -> the real builtins module
        print.__self__.__import__("os") -> the real, unguarded __import__
        print.__self__.open             -> the real open

    which defeated the entire namespace in one expression. Editing the
    module itself means ``__self__`` leads somewhere already safe. The
    worker is dedicated to a single submission, so there is nothing else
    in the process to break.

    ``__import__`` is *replaced* rather than deleted, because the `import`
    statement compiles to a lookup of it: deleting the name does not
    forbid imports, it breaks them, including the ones the rules allow.

    Wrapping every builtin in a Python-level shim would also have closed
    the ``__self__`` route, at the price of turning every ``len()`` and
    ``isinstance()`` in every bot into an extra Python call, against a 2ms
    budget.
    """
    import builtins

    removed = []
    for name in _BANNED_BUILTINS:
        if hasattr(builtins, name):
            try:
                delattr(builtins, name)
                removed.append(name)
            except (AttributeError, TypeError):    # pragma: no cover
                pass
    builtins.__import__ = _guarded_import
    return {"removed": removed, "builtins": builtins}


# ════════════════════════════════════════════════════════════════════
#  AUDIT HOOK
# ════════════════════════════════════════════════════════════════════

#: Event families that reach the operating system. Matched by prefix so
#: that events added in later Python versions inside the same family are
#: denied by default rather than quietly allowed.
_DENY_PREFIXES = (
    "os.", "subprocess.", "socket.", "ctypes.", "marshal.", "winreg.",
    "sqlite3.", "urllib.", "http.", "ftplib.", "smtplib.", "imaplib.",
    "poplib.", "nntplib.", "telnetlib.", "webbrowser.", "shutil.",
    "glob.", "tempfile.", "pty.", "fcntl.", "syslog.", "msvcrt.",
    "mmap.", "cpython.", "code.", "importlib.", "pdb.",
    "ensurepip.", "venv.",
)

#: Individually named events with no useful family prefix.
#:
#: Deliberately absent: `sys.unraisablehook` and `sys.excepthook` fire when
#: an exception is being *reported*, not when a capability is used, so
#: denying them turns every incidental error into a second error inside the
#: hook. `object.__setattr__` fires far too broadly to be a policy. Config
#: immutability is enforced by GameConfig itself, which is the right place.
_DENY_EVENTS = frozenset({
    "open", "builtins.input", "builtins.input/result",
    "sys._getframe", "sys._current_frames", "sys._current_exceptions",
    "sys.settrace", "sys.setprofile", "sys.set_asyncgen_hooks",
    "sys._debugmallocstats", "signal.pthread_kill",
})

# ════════════════════════════════════════════════════════════════════
#  CROSS-DEAL STATE
# ════════════════════════════════════════════════════════════════════

class StateGuard:
    """Restores the allowed modules to their pristine contents each deal.

    Re-executing the submission's own module gives it fresh globals, but
    that is not the whole hiding place. The worker process outlives the
    deal, and so does everything reachable from it:

        import functools
        functools._notes = {}          # deal 1
        functools._notes[opponent]     # deal 2, still there

    ``sys.modules`` is not reset between deals -- only the submission's
    namespace is -- so any module the bot is allowed to import doubles as a
    filing cabinet. The same trick works on a class inside one of those
    modules, and on the ``random`` module's global generator, which is a
    genuine covert channel: seed it with a number in deal 1 and read the
    number back out of the stream in deal 2.

    Restoring costs about a tenth of a millisecond, so it runs
    unconditionally rather than only when something looks wrong. Because it
    works by diffing against a snapshot, it also *reports* what it had to
    undo, which is more useful than a silent clean-up: a submission caught
    parking state is a submission worth looking at by hand.

    This is thorough rather than total. For a guarantee with no reasoning
    attached, `SandboxedBot(fresh_process_per_deal=True)` throws the whole
    interpreter away between deals instead.
    """

    __slots__ = ("_modules", "_classes", "_random")

    def __init__(self, allowed_modules):
        self._modules: list = []
        self._classes: list = []
        for name in sorted(allowed_modules):
            module = sys.modules.get(name)
            if module is None or not hasattr(module, "__dict__"):
                continue
            self._modules.append((name, module, dict(module.__dict__)))
            for attr, obj in list(module.__dict__.items()):
                # Only classes defined *by* this module, so a re-exported
                # name is not snapshotted twice under two owners.
                if not isinstance(obj, type):
                    continue
                if getattr(obj, "__module__", None) != name:
                    continue
                try:
                    members = dict(obj.__dict__)
                except (TypeError, AttributeError):    # pragma: no cover
                    continue
                self._classes.append((f"{name}.{attr}", obj, members))
        self._random = sys.modules.get("random")

    def restore(self, deal_seed: int) -> list[str]:
        """Undo everything the last deal left behind. Returns what it found."""
        tampered: list[str] = []

        for name, module, snapshot in self._modules:
            live = module.__dict__
            if live.keys() != snapshot.keys():
                for key in live.keys() - snapshot.keys():
                    tampered.append(f"{name}.{key}")
            else:
                # Same names can still mean a replaced object, which is the
                # subtler way to park something. Identity is the only honest
                # test here: `==` on an arbitrary object runs its own code.
                for key, original in snapshot.items():
                    if live[key] is not original:
                        tampered.append(f"{name}.{key}")
            live.clear()
            live.update(snapshot)

        for label, cls, snapshot in self._classes:
            live = cls.__dict__
            # Length first: an added attribute always changes it, and this
            # is an O(1) test run over every class in every allowed module.
            if len(live) == len(snapshot):
                continue
            for key in set(live) - set(snapshot):
                tampered.append(f"{label}.{key}")
                try:
                    delattr(cls, key)
                except (AttributeError, TypeError):    # pragma: no cover
                    pass
            for key, original in snapshot.items():
                if key not in live:
                    try:
                        setattr(cls, key, original)
                    except (AttributeError, TypeError):  # pragma: no cover
                        pass

        # Restoring the module dict hands back the same Random instance with
        # its internal state wherever the last deal left it, so the stream
        # has to be reseeded explicitly. A deterministic seed also means a
        # bot leaning on the global generator reproduces across reruns.
        if self._random is not None:
            try:
                self._random.seed(deal_seed)
            except Exception:                          # pragma: no cover
                pass

        return tampered


#: `collections.namedtuple` builds its `__new__` by eval-ing a generated
#: one-line lambda, and `typing.NamedTuple` goes through the same code. Both
#: are ordinary things for a bot to use, and refusing them produces the
#: baffling error "compile() is not allowed" against a submission whose
#: author never wrote compile().
#:
#: The carve-out is deliberately shaped like the template rather than like a
#: permission: a single lambda, under a few hundred characters, calling
#: `_tuple_new`. Granting it costs nothing, because a submission that
#: crafted a matching source would get back a lambda it could equally well
#: have written literally -- no import, no attribute, no capability that
#: was not already reachable. Both halves matter and both are worth
#: re-checking by hand after any change here: that namedtuple still works,
#: and that a general exec() still does not.
_NT_MAX_SOURCE = 4096


def _is_namedtuple_template(source) -> bool:
    if isinstance(source, bytes):
        try:
            source = source.decode("utf-8")
        except UnicodeDecodeError:
            return False
    if not isinstance(source, str) or len(source) > _NT_MAX_SOURCE:
        return False
    if "\n" in source or ";" in source:
        return False
    return source.startswith("lambda _cls,") and "_tuple_new(_cls," in source


def _is_namedtuple_code(code) -> bool:
    """Match the wrapper `eval` builds around the template lambda.

    The `exec` event carries the module-level code object that evaluates
    the expression, not the lambda -- the lambda is one of its constants --
    so the shape to recognise is a tiny `<module>` from `<string>` holding
    exactly one nested lambda that calls `_tuple_new`.
    """
    try:
        if code.co_name != "<module>" or code.co_filename != "<string>":
            return False
        nested = [c for c in code.co_consts if hasattr(c, "co_names")]
        return (len(nested) == 1
                and nested[0].co_name == "<lambda>"
                and "_tuple_new" in nested[0].co_names)
    except (AttributeError, TypeError):
        return False


#: `from __future__ import annotations` stringifies annotations, so anything
#: that needs their *values* has to evaluate them again. `typing.NamedTuple`
#: does exactly that, which is how a class as ordinary as
#:
#:     class Belief(NamedTuple):
#:         mean: float
#:
#: ends up compiling the five-byte source "float" and being told that
#: compile() is not allowed. On 3.14 PEP 563 is redundant with PEP 649, but
#: it is still the house style and still legal, so the pair has to work.
#:
#: Shaped like an annotation rather than like a permission, on the same
#: principle as the namedtuple carve-out above. The load-bearing exclusion
#: is "(": without a call there is no way to invoke anything, so the most
#: this can evaluate is a name, an attribute or a subscript -- all of which
#: the submission could have written literally. "=" is excluded too, so
#: nothing can be bound or walrused into existence.
_ANNOTATION_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "_.[],|*-:'\" "
)
_ANNOTATION_MAX_SOURCE = 256

#: Keywords that would make the source a statement rather than an
#: expression. Excluding them is what stops "import os" -- every character
#: of which is otherwise annotation-shaped -- from reaching compile() at
#: all, rather than relying on the import event to catch it a step later.
#: None/True/False are keywords too but are ordinary inside an annotation
#: (`int | None`), so they stay.
_STATEMENT_KEYWORDS = frozenset(keyword.kwlist) - {"None", "True", "False"}

#: Identifier characters, for splitting a source into bare words.
_WORD_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)


def _is_annotation_expression(source) -> bool:
    if isinstance(source, bytes):
        try:
            source = source.decode("utf-8")
        except UnicodeDecodeError:
            return False
    if not isinstance(source, str):
        return False
    if not source or len(source) > _ANNOTATION_MAX_SOURCE:
        return False
    if not all(ch in _ANNOTATION_CHARS for ch in source):
        return False

    word = ""
    for ch in source + " ":
        if ch in _WORD_CHARS:
            word += ch
            continue
        if word in _STATEMENT_KEYWORDS:
            return False
        word = ""
    return True


def _is_annotation_code(code) -> bool:
    """Match the code object compiled from such an expression.

    Sound only in combination with the compile gate above: `compile` is the
    only route by which a submission can obtain a ``<string>`` code object
    at all, so anything reaching here has already been checked at the
    source level and cannot contain a call.
    """
    try:
        if code.co_name != "<module>" or code.co_filename != "<string>":
            return False
        return not any(hasattr(c, "co_names") for c in code.co_consts)
    except (AttributeError, TypeError):
        return False


#: The module name submission code runs under. Only one module ever holds
#: it, because the worker execs exactly one submission.
SUBMISSION_MODULE = "__submission__"


def pin_namedtuple_module(module_name: str = SUBMISSION_MODULE):
    """Stop `namedtuple` needing the caller's frame to name its class.

    `collections.namedtuple` sets the new class's ``__module__`` by looking
    at whoever called it. On 3.12+ that is `sys._getframemodulename`, which
    carries no audit event. On 3.11 and earlier there is no such function,
    so it falls back to ``sys._getframe(1)`` -- which the hook refuses,
    because frame walking is how a submission would go looking for the
    pipe.

    The result was a bot using `namedtuple` being rejected on 3.11 and
    accepted on 3.13, with an error naming `sys._getframe`, which the author
    never wrote. Passing `module` explicitly means neither branch is
    reached on any version, and the value matches what 3.12+ would have
    computed anyway.

    Must run before StateGuard snapshots `collections`, so that the
    restore between deals preserves this rather than reporting it as
    tampering.
    """
    import collections

    real = collections.namedtuple
    if getattr(real, "__sandbox_pinned__", False):
        return real

    def namedtuple(typename, field_names, *, rename=False, defaults=None,
                   module=None):
        return real(typename, field_names, rename=rename,
                    defaults=defaults,
                    module=module_name if module is None else module)

    namedtuple.__name__ = real.__name__
    namedtuple.__qualname__ = real.__qualname__
    namedtuple.__doc__ = real.__doc__
    namedtuple.__sandbox_pinned__ = True
    collections.namedtuple = namedtuple
    return namedtuple


class LockdownReport:
    """What lockdown actually managed to apply, for logging and tests."""

    __slots__ = ("purged", "removed_builtins", "hook_installed", "limits")

    def __init__(self, purged, removed_builtins, hook_installed, limits):
        self.purged = purged
        self.removed_builtins = removed_builtins
        self.hook_installed = hook_installed
        self.limits = limits

    def __repr__(self) -> str:
        return (f"LockdownReport(purged={len(self.purged)}, "
                f"builtins_removed={len(self.removed_builtins)}, "
                f"hook={self.hook_installed}, limits={self.limits})")


def _install_audit_hook(allowed_code) -> None:
    """Install the irreversible capability hook.

    `allowed_code` is the set of code objects the worker compiled itself,
    before lockdown. Executing one of those is how the submission's module
    is (re-)run each deal; executing anything else is a submission calling
    exec(), which is refused.

    The hook must not itself trigger an audited event -- no imports, no
    file access, no formatting that could touch linecache -- or it
    recurses. Everything it touches is a frozenset lookup or a string
    comparison.
    """
    deny_prefixes = _DENY_PREFIXES
    deny_events = _DENY_EVENTS
    pickle_modules = _PICKLE_MODULES
    allowed_roots = IMPORTABLE_MODULES
    allowed_listing = ", ".join(sorted(ALLOWED_MODULES))
    code_allowset = frozenset(map(id, allowed_code))
    # Held so the code objects cannot be garbage collected and have their
    # ids reused by something the submission constructs later.
    _pin = tuple(allowed_code)

    def _hook(event, args):
        if event == "import":
            # Every module a submission may touch was imported before the
            # purge, so in practice this event only fires for something
            # outside the allowlist. The root check is here for the
            # remaining case -- a submodule nobody pre-imported -- and it
            # is trustworthy in a way a source scan is not, because the
            # name has already been resolved by the import machinery and
            # cannot be assembled at runtime to dodge it.
            if args and str(args[0]).split(".")[0] in allowed_roots:
                return
            raise SandboxViolation(
                f"import of {args[0]!r} is not allowed. Permitted modules: "
                + allowed_listing
            )
        if event == "exec":
            if args and id(args[0]) in code_allowset:
                return
            if args and (_is_namedtuple_code(args[0])
                         or _is_annotation_code(args[0])):
                return
            raise SandboxViolation("exec()/eval() is not allowed in a submission")
        if event == "compile":
            if args and (_is_namedtuple_template(args[0])
                         or _is_annotation_expression(args[0])):
                return
            raise SandboxViolation("compile() is not allowed in a submission")
        if event == "pickle.find_class":
            if args and args[0] in pickle_modules:
                return
            raise SandboxViolation(f"unpickling from {args[0]!r} is not allowed")
        if event in deny_events:
            raise SandboxViolation(f"{event} is not allowed in a submission")
        for prefix in deny_prefixes:
            if event.startswith(prefix):
                raise SandboxViolation(f"{event} is not allowed in a submission")

    _hook.__pinned_code__ = _pin
    sys.addaudithook(_hook)


# ════════════════════════════════════════════════════════════════════
#  ORCHESTRATION
# ════════════════════════════════════════════════════════════════════

#: Pre-imported alongside the allowlist. `collections.abc` is not reached
#: by `import collections`, and `from collections.abc import Sequence` is
#: ordinary code that should not have to survive an audit event to work.
_PREIMPORT = ("collections.abc",) + tuple(sorted(_LANGUAGE_MODULES))


def preimport_allowed() -> None:
    """Warm the cache for every module a submission may legally import.

    Without this the first `import math` in a submission goes through the
    import machinery and past the audit hook, which works but is needless
    exposure, and it leaves StateGuard with nothing to snapshot -- module
    globals that are not loaded yet cannot be restored between deals.
    """
    for name in sorted(ALLOWED_MODULES) + list(_PREIMPORT):
        try:
            _REAL_IMPORT(name)
        except ImportError:                        # pragma: no cover
            pass


def lock_down(allowed_code=(), *, extra_keep=frozenset(),
              limits_applied=()) -> LockdownReport:
    """Seal this process. Irreversible; call once, before submission code.

    Order matters:

      1. pre-import the allowlist, so it survives the purge warm
      2. purge the module cache, so the audit hook's refusal of `import`
         bites on everything else
      3. rewrite the builtins at the root, closing the ``__self__`` route
      4. install the hook, after which nothing can be relaxed again

    Anything the caller needs after this point must already be imported
    and referenced, because it will not be importable again.
    """
    preimport_allowed()
    keep = IMPORTABLE_MODULES | _WORKER_ESSENTIAL | frozenset(extra_keep)
    purged = purge_modules(keep)
    builtins_result = restrict_builtins()
    _install_audit_hook(allowed_code)
    return LockdownReport(
        purged=purged,
        removed_builtins=builtins_result["removed"],
        hook_installed=True,
        limits=tuple(limits_applied),
    )
