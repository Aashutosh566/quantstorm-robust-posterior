"""
bot_loader.py — Divided Oracle: Submission loading & static validation
=======================================================================

Two jobs:

  1. `check_source()`  — read a submission and decide whether it is legal,
     WITHOUT running it. This is the gate the tournament applies to every
     entry before the round robin starts.

  2. `load_bot_class()` — execute a submission in this process and hand back
     its Bot class. Convenient for the backtester and the tests; NOT how the
     tournament runs entries. See the warning below.

WHERE THE ENFORCEMENT ACTUALLY LIVES
------------------------------------

The static scan below is a fast, legible gate that rejects the obvious
attacks with a clear message the author can act on. It is not, and cannot
be, the security boundary: `__import__("os")` is a call rather than an
import statement, `getattr(x, "__cl" + "ass__")` defeats any name-based
check, and nothing static can bound runtime.

The boundary is `sandbox.py`, which runs each submission in a separate
process with resource limits, so the engine's hands and hidden score are
not merely off-limits but absent. `load_bot_class()` gives you neither.
Use it on code you trust; use SandboxedBot on code you do not.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from dataclasses import dataclass, field
from pathlib import Path

from policy import ALLOWED_MODULES, IMPORTABLE_MODULES

__all__ = [
    "ALLOWED_MODULES", "IMPORTABLE_MODULES", "CheckResult", "check_source",
    "load_bot_class", "load_for_testing", "REQUIRED_METHODS",
]

#: Imported, not restated. This file and policy.py used to hold two
#: separate literals with a comment asking whoever edited one to remember
#: the other, which is not a mechanism -- and the failure is silent in the
#: worst direction: a module the gate accepts and the runtime then refuses
#: rejects an entry at play time, after the author has gone home.

#: Methods every submission must define. `bid` used to be missing from this
#: list AND from the participant README, so a bot could load, silently never
#: bid, and forfeit the entire auction layer without a word of warning.
REQUIRED_METHODS = ("reset", "bid", "quote", "respond", "use_transform")

#: Builtins that exist, in a submission, only to get around the rules.
_BANNED_CALLS = {
    "__import__", "eval", "exec", "compile", "open", "input",
    "globals", "vars", "breakpoint", "memoryview",
}

#: Attribute names that walk out of the object graph into the interpreter.
#: `().__class__.__mro__[1].__subclasses__()` reaches Popen from a literal;
#: `f.f_back` walks the call stack; `__globals__` reaches another module's
#: builtins. None of these have a legitimate use in a trading bot.
_BANNED_ATTRS = {
    "__class__", "__bases__", "__mro__", "__subclasses__", "__globals__",
    "__builtins__", "__code__", "__closure__", "__dict__", "__getattribute__",
    "__reduce__", "__reduce_ex__", "__loader__", "__spec__",
    "f_back", "f_locals", "f_globals", "f_builtins", "tb_frame", "tb_next",
    "gi_frame", "cr_frame", "_getframe", "__traceback__",
}


@dataclass
class CheckResult:
    """Verdict on one submission file.

    Failures carry a kind, because the tools want different subsets:

        "code"      the file does something it must not (illegal import,
                    an escape hatch, a syntax error). Never runnable.
        "interface" a required method is missing. The engine has a
                    documented fallback for each, so this is playable for
                    local testing but not acceptable as an entry.
        "meta"      paperwork: the three header comments. Playable, and
                    the reason a submission gets handed back.
    """
    path: Path
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def fail(self, msg: str, kind: str = "code") -> None:
        self.ok = False
        self.errors.append(msg)
        self.kinds.append(kind)

    def blocking(self, kinds: tuple[str, ...]) -> list[str]:
        """Errors of the given kinds."""
        return [e for e, k in zip(self.errors, self.kinds) if k in kinds]

    def report(self) -> str:
        lines = [f"{self.path.name}: {'OK' if self.ok else 'REJECTED'}"]
        lines += [f"    error:   {e}" for e in self.errors]
        lines += [f"    warning: {w}" for w in self.warnings]
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
#  STATIC CHECKS
# ════════════════════════════════════════════════════════════════════

_META_FIELDS = ("Name", "College", "Roll Number")


def _check_metadata(source: str, result: CheckResult) -> None:
    """The three header comments are mandatory, as the rules say they are.

    They used to be documented as mandatory and implemented as a printed
    warning, which meant an anonymous entry sailed into the standings.
    """
    for label in _META_FIELDS:
        pattern = rf"#\s*{label}\s*:\s*(.+)"
        m = re.search(pattern, source, re.IGNORECASE)
        value = (m.group(1).strip() if m else "")
        placeholder = value.startswith("[") or value.lower() in {
            "", "your name here", "your college here", "your roll number here",
        }
        if not m:
            result.fail(f"missing the mandatory '# {label}:' header comment", "meta")
        elif placeholder:
            result.fail(
                f"'# {label}:' is still the template placeholder ({value!r})", "meta")
        else:
            result.metadata[label] = value


def _check_ast(source: str, result: CheckResult) -> ast.Module | None:
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        result.fail(f"syntax error on line {e.lineno}: {e.msg}")
        return None

    for node in ast.walk(tree):
        # --- imports -------------------------------------------------
        # Checked against IMPORTABLE_MODULES but *reported* in terms of
        # ALLOWED_MODULES: the extra entries are language machinery nobody
        # sets out to import, so listing them would only invite questions.
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in IMPORTABLE_MODULES:
                    result.fail(
                        f"line {node.lineno}: illegal import {alias.name!r}. "
                        f"Allowed: {', '.join(sorted(ALLOWED_MODULES))}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0:
                result.fail(f"line {node.lineno}: relative imports are not allowed")
            elif node.module:
                root = node.module.split(".")[0]
                if root not in IMPORTABLE_MODULES:
                    result.fail(
                        f"line {node.lineno}: illegal import from {node.module!r}. "
                        f"Allowed: {', '.join(sorted(ALLOWED_MODULES))}"
                    )

        # --- calls to escape-hatch builtins --------------------------
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else None
            if name in _BANNED_CALLS:
                result.fail(
                    f"line {node.lineno}: {name}() is not allowed in a submission"
                )

        # --- interpreter-level attribute access ----------------------
        elif isinstance(node, ast.Attribute):
            if node.attr in _BANNED_ATTRS:
                result.fail(
                    f"line {node.lineno}: access to {node.attr!r} is not allowed "
                    f"(it reaches outside the game)"
                )

        # --- the same names as bare strings, e.g. getattr(x, "__class__")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in _BANNED_ATTRS or node.value in _BANNED_CALLS:
                result.warnings.append(
                    f"line {node.lineno}: the string {node.value!r} looks like an "
                    f"attempt to reach interpreter internals by name"
                )

    return tree


def _check_interface(tree: ast.Module, result: CheckResult) -> None:
    """Confirm a class named Bot exists and defines the required methods."""
    bot = next(
        (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Bot"),
        None,
    )
    if bot is None:
        result.fail("no class named 'Bot' at module level", "interface")
        return

    defined = {
        n.name for n in bot.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = [m for m in REQUIRED_METHODS if m not in defined]
    if missing:
        result.fail(
            f"Bot is missing required method(s): {', '.join(missing)}. "
            f"See starter_bot.py -- every one of {', '.join(REQUIRED_METHODS)} "
            f"must be defined.",
            "interface",
        )


def check_source(filepath: str | Path) -> CheckResult:
    """Validate a submission without executing a line of it."""
    path = Path(filepath)
    result = CheckResult(path=path)

    if not path.exists():
        result.fail("file not found")
        return result
    if path.suffix != ".py":
        result.fail("submission must be a .py file")
        return result

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        result.fail(f"could not read file: {e}")
        return result

    if not source.strip():
        result.fail("file is empty")
        return result

    _check_metadata(source, result)
    tree = _check_ast(source, result)
    if tree is not None:
        _check_interface(tree, result)
    return result


def validate_source_code(filepath: Path) -> None:
    """Backwards-compatible wrapper: raise ValueError on a rejected file."""
    result = check_source(filepath)
    if not result.ok:
        raise ValueError("; ".join(result.errors))


# ════════════════════════════════════════════════════════════════════
#  IN-PROCESS LOADING  (trusted code only)
# ════════════════════════════════════════════════════════════════════

def load_bot_class(filepath: str | Path, *, strict: bool = True) -> type:
    """Execute a submission HERE and return its Bot class.

    This runs the file's top-level code in the calling process with no
    isolation whatsoever. Use it for your own reference bots and in tests.
    For anything a stranger sent you, use sandbox.SandboxedBot instead.

    strict=False downgrades metadata/interface complaints to warnings, which
    is what the backtester wants while a participant is still writing.
    """
    path = Path(filepath)
    result = check_source(path)
    if not result.ok:
        # Strict rejects everything. Lenient blocks only what the code
        # DOES: a missing method or an unfilled header should not stop a
        # participant duelling a half-written bot, but an illegal import
        # is never runnable.
        kinds = ("code", "interface", "meta") if strict else ("code",)
        blocking = result.blocking(kinds)
        if blocking:
            raise ValueError(f"{path.name}: " + "; ".join(blocking))

    module_name = f"_bot_{path.stem}_{abs(hash(str(path.resolve())))}"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load module from: {path}")

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise ImportError(f"error executing {path.name}: {type(e).__name__}: {e}")

    if not hasattr(module, "Bot"):
        raise AttributeError(
            f"{path.name} does not define a 'Bot' class. "
            f"Available names: {[n for n in dir(module) if not n.startswith('_')]}"
        )

    bot_cls = module.Bot
    missing = [m for m in REQUIRED_METHODS if not callable(getattr(bot_cls, m, None))]
    if missing and strict:
        raise AttributeError(
            f"{path.name}: Bot class missing required methods: {missing}"
        )
    return bot_cls


def load_for_testing(filepath: str | Path, quiet: bool = False) -> type:
    """Load a bot for the participant-facing tools.

    Deliberately lenient about paperwork so that a half-finished bot can
    still be duelled -- but it says out loud what the tournament would do
    with the file, because finding that out on submission day is too late.
    """
    path = Path(filepath)
    result = check_source(path)
    if not result.ok and not quiet:
        print(f"  ⚠  {path.name} would be REJECTED by the tournament:")
        for e in result.errors:
            print(f"       • {e}")
        print("     (running it here anyway)")
    for w in result.warnings if not quiet else ():
        print(f"  ⚠  {path.name}: {w}")
    return load_bot_class(path, strict=False)
