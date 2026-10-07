#!/usr/bin/env python3
"""Prove-It pytest gate: the agent cannot finish while your tests fail.

Claude Code runs this as a Stop hook (when the agent is about to say "done").
  0. At session start (SessionStart hook, --baseline) it records the content of every relevant file
     (SHA-256 of the working-tree bytes), the test command, and the content of every test file.
  1. If nothing relevant changed since that baseline, or this exact state already passed and no later
     run failed it -> allow, no test run. No baseline (or it failed) -> the tests run.
  2. Otherwise run the test command. Green -> allow. Red -> block the stop and hand the failure back to
     the agent, at most 2 times in a row; then let it stop with a visible "NOT verified" warning.
Every test run is appended to .prove-it/gate.jsonl and .prove-it/gate-log.md.

Config (environment first, then .claude/prove-it/config.json):
  PROVE_IT_TEST_CMD / "test_cmd"   test command (default: python -m pytest -q, using .venv if present)
  PROVE_IT_TIMEOUT  / "timeout"    seconds for one test run, 1..570 (the hook itself is stopped at 600)
  PROVE_IT_WATCH    / "watch"      comma-separated globs of relevant files
  PROVE_IT_GATE_DISABLE=1          switch the hook off (a manual --check still runs the tests)
The number of fix cycles is fixed at 2.
Manual run (always runs the tests): python3 pytest_gate.py --check   exit 0 green, 1 red, 2 error
"""
from __future__ import annotations

import datetime as _dt
import errno
import fcntl
import fnmatch
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_CYCLES = 2
HOOK_TIMEOUT = 600          # the installed Stop hook's timeout
MAX_TEST_TIMEOUT = 570      # the inner test run must end (and be cleaned up) before the hook is killed
DEFAULT_WATCH = ["*.py", "pyproject.toml", "setup.cfg", "setup.py", "pytest.ini", "tox.ini", "requirements*.txt",
                 "*.toml", "*.cfg", "*.ini", "*.json", "*.yaml", "*.yml"]
IGNORE_DIRS = {".prove-it", ".claude", ".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache",
               ".mypy_cache", ".ruff_cache", "dist", "build", ".tox"}
TEST_FILE = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|conftest\.py$)")
STATE_FILES = ("gate-state.json", "gate-state.lock", "gate.jsonl", "gate-log.md", "gate-errors.log")


class ConfigError(Exception):
    pass


class GateError(Exception):
    """Anything that prevents a trustworthy decision. Never treated as green."""


def now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


# ---------- state files: one validated directory descriptor, no symlinks anywhere ----------

class StateDir:
    """.prove-it/ opened once with O_DIRECTORY|O_NOFOLLOW and used through its descriptor for every open,
    temp file and replace, so swapping the folder for a symlink mid-run cannot redirect any write."""

    def __init__(self, root: Path):
        path = root / ".prove-it"
        try:
            os.mkdir(path)
        except FileExistsError:
            pass
        try:
            self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                raise GateError(".prove-it is a symlink or not a folder; the gate refuses to use it")
            raise

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def open(self, name: str, flags: int, mode: int = 0o644) -> int:
        # O_NONBLOCK: a FIFO or device planted here must not hang the open before its type is checked
        try:
            fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=self.fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                raise GateError(f"{name} is a symlink; the gate refuses to follow it")
            if exc.errno == errno.ENXIO:
                raise GateError(f"{name} is not a regular file")
            raise
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise GateError(f"{name} is not a regular file")
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
        return fd

    def exists(self, name: str) -> bool:
        try:
            os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def read(self, name: str) -> str:
        try:
            fd = self.open(name, os.O_RDONLY)
        except FileNotFoundError:
            return ""
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            return f.read()

    def append(self, name: str, text: str) -> None:
        fd = self.open(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(text)

    def replace(self, name: str, text: str) -> None:
        tmp = f".{name}.{os.urandom(6).hex()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=self.fd)
            except FileNotFoundError:
                pass
            raise


# ---------- config ----------

def load_config(root: Path) -> dict:
    """Validate every supplied value BEFORE any default applies. Invalid = ConfigError, never a guess."""
    cfg = {}
    f = root / ".claude" / "prove-it" / "config.json"
    if f.is_symlink():
        raise ConfigError(f"{f} is a symlink")
    if f.exists():
        try:
            cfg = json.loads(f.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ConfigError(f"{f} is not valid JSON: {exc}")
        if not isinstance(cfg, dict):
            raise ConfigError(f"{f} must contain a JSON object")
    env = os.environ
    if "PROVE_IT_TEST_CMD" in env:
        test_cmd = env["PROVE_IT_TEST_CMD"]
    elif "test_cmd" in cfg:
        test_cmd = cfg["test_cmd"]
    else:
        test_cmd = default_test_cmd(root)
    if not isinstance(test_cmd, str) or not test_cmd.strip():
        raise ConfigError("test_cmd must be a non-empty string (an empty command would always look green)")
    if "PROVE_IT_TIMEOUT" in env:
        try:
            timeout = float(env["PROVE_IT_TIMEOUT"])
        except ValueError:
            raise ConfigError(f"PROVE_IT_TIMEOUT={env['PROVE_IT_TIMEOUT']!r} is not a number")
    else:
        timeout = cfg.get("timeout", 300)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ConfigError(f"timeout must be a number of seconds, got {timeout!r}")
        timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_TEST_TIMEOUT:
        raise ConfigError(f"timeout must be a finite number of seconds between 1 and {MAX_TEST_TIMEOUT} "
                          f"(the hook itself is stopped after {HOOK_TIMEOUT})")
    if "max_cycles" in cfg and (isinstance(cfg["max_cycles"], bool) or cfg["max_cycles"] != MAX_CYCLES):
        raise ConfigError(f"max_cycles is fixed at {MAX_CYCLES} in Mini; remove it from the config")
    if "PROVE_IT_WATCH" in env:
        watch = [w.strip() for w in env["PROVE_IT_WATCH"].split(",") if w.strip()]
    else:
        watch = cfg.get("watch", DEFAULT_WATCH)
    if not isinstance(watch, list) or not watch or not all(isinstance(w, str) and w.strip() for w in watch):
        raise ConfigError("watch must be a non-empty list of glob strings")
    return {"test_cmd": test_cmd, "timeout": timeout, "watch": watch}


def default_test_cmd(root: Path) -> str:
    for venv in (".venv", "venv"):
        if (root / venv / "bin" / "python").exists():
            return f"{venv}/bin/python -m pytest -q"
    return f"{'python3' if shutil.which('python3') else 'python'} -m pytest -q"


def is_direct_pytest(cmd: str) -> bool:
    """Exit 5 = 'no tests collected' only for a plain pytest call: `pytest ...`, `python -m pytest ...`
    or `<path>/python -m pytest ...`, with no shell operators. Anything else: exit 5 is a failure."""
    if re.search(r"[;&|<>`$()\n]", cmd):
        return False
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return False
    if not argv:
        return False
    first = os.path.basename(argv[0])
    if first in ("pytest", "py.test"):
        return True
    return bool(re.fullmatch(r"python(\d+(\.\d+)*)?", first)) and argv[1:3] == ["-m", "pytest"]


# ---------- snapshot of the working tree ----------

def git_strict(root: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError(f"git {args[0]} failed: {exc}")
    if r.returncode != 0:
        raise GateError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def in_git(root: Path) -> bool:
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"


def all_files(root: Path) -> list[str]:
    if in_git(root):
        out = git_strict(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        return sorted({n for n in out.split("\0") if n})
    names = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        names += [os.path.relpath(os.path.join(dirpath, f), root) for f in filenames]
    return sorted(names)


def ignored(path: str) -> bool:
    parts = Path(path).parts
    return any(p in IGNORE_DIRS for p in parts[:-1]) or (bool(parts) and parts[0] in IGNORE_DIRS)


def relevant(files: list[str], watch: list[str]) -> list[str]:
    return [f for f in files if not ignored(f)
            and any(fnmatch.fnmatch(Path(f).name, w) or fnmatch.fnmatch(f, w) for w in watch)]


def content_hash(root: Path, rel: str) -> str:
    """SHA-256 of what the path really resolves to. Containment is checked FIRST (through symlinked files and
    parent folders, existing or not); anything outside the repository or not readable is unverifiable: it gets a
    value that never matches, so the tests always run and nothing is cached."""
    unverifiable = "<unverifiable:" + os.urandom(8).hex() + ">"
    real_root = Path(os.path.realpath(root))
    target = Path(os.path.realpath(root / rel))
    if real_root not in target.parents:
        return unverifiable
    try:
        st = os.stat(target)
    except FileNotFoundError:
        # only a plain absence counts as deleted: any symlink on the way (the file or a parent folder)
        # that points nowhere, or any other error, makes the path unverifiable
        cur = root
        for part in Path(rel).parts:
            cur = cur / part
            try:
                st_l = os.lstat(cur)
            except FileNotFoundError:
                return "<deleted>"                 # this component really doesn't exist (inside the repo)
            except OSError:
                return unverifiable
            if stat.S_ISLNK(st_l.st_mode):
                return unverifiable                # a dangling or unresolvable symlink on the path
        return unverifiable
    except OSError:
        return unverifiable
    if not stat.S_ISREG(st.st_mode):
        return unverifiable
    try:
        data = target.read_bytes()
    except OSError:
        return unverifiable
    prefix = "link:" if target != Path(os.path.abspath(root / rel)) else ""
    return prefix + hashlib.sha256(data).hexdigest()


def snapshot(root: Path, cfg: dict) -> tuple[str, dict[str, str]]:
    """(state fingerprint, {test file: sha256}). SHA-256 of the actual working-tree bytes of every relevant
    file plus the test command; committing or staging never changes it, editing relevant content does."""
    files = all_files(root)
    h = hashlib.sha256(cfg["test_cmd"].encode() + b"\0")
    for rel in relevant(files, cfg["watch"]):
        h.update(rel.encode() + b"\0" + content_hash(root, rel).encode() + b"\0")
    tests = {rel: content_hash(root, rel) for rel in files if TEST_FILE.search(rel) and not ignored(rel)}
    return h.hexdigest()[:24], tests


def changed_tests(baseline_tests: dict | None, tests: dict) -> list[str]:
    if not isinstance(baseline_tests, dict):
        return []
    changed = [p for p, digest in tests.items() if baseline_tests.get(p) != digest]
    deleted = [p for p in baseline_tests if p not in tests or tests[p] == "<deleted>"]
    return sorted(set(changed) | set(deleted))


# ---------- shared state, serialised by a lock ----------

class State:
    """Per-session entries plus a shared record of known-red states with a monotonic red generation.
    Saves happen under a lock and merge into the file as it is NOW (a session writes only its own entry).
    A pass is recorded as green only if no red result for the same state was recorded after the run began."""

    def __init__(self, root: Path, session: str):
        self.dir = StateDir(root)
        self.session = re.sub(r"[^A-Za-z0-9._-]", "_", session or "manual")[:80]
        self._forget: set[str] = set()
        self._red: set[str] = set()
        self._green: set[str] = set()
        data = self._load()
        self.s = dict(data.get("sessions", {}).get(self.session) or {"blocks": 0, "last_green": ""})
        self.start_gen = int(data.get("red_gen", 0))
        self._new_baseline = False

    def _load(self) -> dict:
        try:
            data = json.loads(self.dir.read("gate-state.json") or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict) or not isinstance(data.get("sessions", {}), dict) \
                or not isinstance(data.get("known_red", {}), dict):
            data = {}
        data.setdefault("sessions", {})
        data.setdefault("known_red", {})
        data.setdefault("red_gen", 0)
        if not isinstance(data.get("red_hist"), dict):
            data["red_hist"] = {}
        return data

    def latest_baseline(self) -> dict | None:
        """The most recently recorded session baseline (used by a manual --check)."""
        best = None
        for entry in self._load()["sessions"].values():
            b = entry.get("baseline") if isinstance(entry, dict) else None
            if isinstance(b, dict) and (best is None or (int(b.get("seq", 0) or 0), str(b.get("time", "")))
                                        > (int(best.get("seq", 0) or 0), str(best.get("time", "")))):
                best = b
        return best

    def known_red(self) -> set:
        return set(self._load().get("known_red", {}))

    def is_green(self, fp: str) -> bool:
        return self.s.get("last_green") == fp and fp not in self.known_red()

    def forget_green(self, fp: str) -> None:
        self._forget.add(fp)
        self._red.add(fp)
        if self.s.get("last_green") == fp:
            self.s["last_green"] = ""

    def mark_green(self, fp: str) -> None:
        self.s["last_green"] = fp
        self._green.add(fp)
        self._red.discard(fp)

    def save(self) -> bool:
        """Returns False if a green result was refused because a newer red run recorded this state."""
        lock_fd = self.dir.open("gate-state.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        accepted = True
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            data = self._load()                                   # reload under the lock, then merge
            for fp in self._green:
                entry = data["known_red"].get(fp)
                gen = entry.get("gen", 0) if isinstance(entry, dict) else 0
                gen = max(gen, int(data["red_hist"].get(fp, 0) or 0))   # history outlives the known-red entry
                if gen > self.start_gen:
                    accepted = False                              # a newer red run saw this state fail
                    if self.s.get("last_green") == fp:
                        self.s["last_green"] = ""
                else:
                    data["known_red"].pop(fp, None)
            for fp in self._red:
                data["red_gen"] = int(data.get("red_gen", 0)) + 1
                data["known_red"][fp] = {"gen": data["red_gen"], "time": now()}
                data["red_hist"].pop(fp, None)
                data["red_hist"][fp] = data["red_gen"]                 # latest red generation, kept after clearing
            for name, entry in data["sessions"].items():
                if isinstance(entry, dict) and entry.get("last_green") in self._forget:
                    entry["last_green"] = ""
            if self._new_baseline and isinstance(self.s.get("baseline"), dict):
                data["baseline_seq"] = int(data.get("baseline_seq", 0) or 0) + 1   # strictly increasing
                self.s["baseline"]["seq"] = data["baseline_seq"]
                self._new_baseline = False
            data["sessions"][self.session] = self.s
            data["sessions"] = dict(list(data["sessions"].items())[-50:])
            data["known_red"] = dict(list(data["known_red"].items())[-200:])
            data["red_hist"] = dict(list(data["red_hist"].items())[-200:])
            self.dir.replace("gate-state.json", json.dumps(data, indent=1))
            self._forget, self._red, self._green = set(), set(), set()
        finally:
            os.close(lock_fd)
        return accepted

    def log(self, event: dict) -> None:
        event = {"time": now(), "session": self.session, **event}
        self.dir.append("gate.jsonl", json.dumps(event) + "\n")
        icon = {"pass": "PASS", "block": "BLOCKED", "gave_up": "GAVE UP", "skip": "NO TESTS", "check": "CHECK",
                "interrupted": "INTERRUPTED", "unverified": "NOT VERIFIED"}.get(event["decision"], "?")
        line = f"- {event['time']} **{icon}** {event.get('note', '')}"
        if event.get("failed"):
            line += " — failing: " + ", ".join(event["failed"][:5])
        if event.get("tests_touched"):
            line += " — test files changed: " + ", ".join(event["tests_touched"][:5])
        header = "" if self.dir.exists("gate-log.md") else "# Prove-It gate log\n\nEvery test run:\n\n"
        self.dir.append("gate-log.md", header + line + "\n")

    def close(self) -> None:
        self.dir.close()


# ---------- running the tests ----------

class Terminated(Exception):
    pass


def _raise_terminated(signum, _frame):
    raise Terminated(signum)


def run_tests(cmd: str, root: Path, timeout: float) -> tuple[int, str, float]:
    t0 = time.time()
    pyc = tempfile.mkdtemp(prefix="prove-it-pyc-")  # fresh bytecode cache: no stale .pyc results
    env = dict(os.environ, PYTHONPYCACHEPREFIX=pyc, PYTHONDONTWRITEBYTECODE="1")
    old = {s: signal.signal(s, _raise_terminated) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    p = None
    try:
        # own process group: on timeout or when the hook itself is stopped, the whole group is killed
        p = subprocess.Popen(cmd, shell=True, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             env=env, stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            out, _ = p.communicate(timeout=timeout)
            code = p.returncode
        except subprocess.TimeoutExpired:
            kill_group(p)
            code, out = 124, f"test run timed out after {timeout:.0f}s (process group killed)"
        return code, out or "", time.time() - t0
    finally:
        if p is not None and p.poll() is None:
            kill_group(p)
        elif p is not None:
            try:
                os.killpg(p.pid, signal.SIGKILL)   # children left in the group after the shell exited
            except (ProcessLookupError, PermissionError):
                pass
        for s, h in old.items():
            signal.signal(s, h)
        shutil.rmtree(pyc, ignore_errors=True)


def kill_group(p: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(p.pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        try:
            p.wait(timeout=3)
            break
        except subprocess.TimeoutExpired:
            continue
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        p.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def failure_summary(output: str, lines: int = 60) -> str:
    out = output.strip().splitlines()
    summary = [ln for ln in out if ln.startswith(("FAILED ", "ERROR ")) or "short test summary" in ln]
    tail = out[-lines:]
    seen = set(tail)
    return "\n".join([ln for ln in summary if ln not in seen][:20] + tail)


# ---------- decisions ----------

def record_baseline(root: Path, session: str) -> None:
    """SessionStart: remember the state the session started from (the first successful call wins)."""
    st = State(root, session)
    try:
        if not st.s.get("baseline"):
            cfg = load_config(root)
            fp, tests = snapshot(root, cfg)
            st.s["baseline"] = {"fp": fp, "tests": tests, "time": now()}
            st._new_baseline = True
            st.save()
    finally:
        st.close()


def edit_note(touched: list[str]) -> str:
    if not touched:
        return ""
    return (" Test files changed in this session: " + ", ".join(touched[:5]) + (" ..." if len(touched) > 5 else "")
            + ". Check that they were not weakened.")


def decide(root: Path, session: str, manual: bool = False) -> dict:
    st = State(root, session)
    try:
        return _decide(root, st, manual)
    finally:
        st.close()


def gate_deadline() -> float:
    """One monotonic deadline for the whole gate (all attempts), below the hook's own 600 s timeout.
    PROVE_IT_GATE_DEADLINE can only lower it (used by the kit's tests)."""
    total = float(MAX_TEST_TIMEOUT)
    raw = os.environ.get("PROVE_IT_GATE_DEADLINE")
    if raw:
        try:
            total = min(total, max(float(raw), 0.1))
        except ValueError:
            pass
    return time.monotonic() + total


def _decide(root: Path, st: State, manual: bool) -> dict:
    """Return the hook output dict ({} = allow silently)."""
    deadline = gate_deadline()
    cfg = load_config(root)
    fp, tests = snapshot(root, cfg)
    own = st.s.get("baseline") if isinstance(st.s.get("baseline"), dict) else None
    baseline = st.latest_baseline() if manual else own

    def touched_now(test_hashes: dict) -> list:
        return changed_tests(baseline.get("tests") if baseline else None, test_hashes)

    touched = touched_now(tests)
    if not manual:
        if own and own.get("fp") == fp:                # nothing relevant changed in this session: no test run
            note = edit_note(touched)
            return {"systemMessage": "Prove-It gate: no relevant change, no test run." + note} if note else {}
        if st.is_green(fp):                            # this exact state passed and nothing failed it since
            note = edit_note(touched)
            return {"systemMessage": "Prove-It gate: this state already passed (no new test run)." + note} if note else {}
    changed_during, no_time, ran = False, False, False
    code, out, secs = 1, "", 0.0
    start_fp = fp
    for attempt in (1, 2):                             # one rerun if files change while the tests run
        remaining = deadline - time.monotonic()
        if remaining <= 1.0 or (attempt == 2 and remaining < max(5.0, secs)):
            no_time = True                             # never start an attempt that can't finish in time
            break
        ran = True
        start_fp = fp
        t_start = time.monotonic()
        try:
            code, out, secs = run_tests(cfg["test_cmd"], root, max(min(cfg["timeout"], remaining), 0.1))
        except Terminated as exc:                      # run_tests has already killed the process group
            sig = int(exc.args[0]) if exc.args else int(signal.SIGTERM)
            try:
                touched = touched_now(snapshot(root, cfg)[1])
            except Exception:
                pass                                   # best effort: keep the pre-run list
            st.log({"cmd": cfg["test_cmd"], "decision": "interrupted", "fingerprint": start_fp, "attempt": attempt,
                    "exit": 128 + sig, "seconds": round(time.monotonic() - t_start, 1), "tests_touched": touched,
                    "note": f"the hook was stopped (signal {sig}) while the tests ran; nothing was verified"})
            raise
        if code != 0:
            st.forget_green(start_fp)                  # persist the red BEFORE anything else can fail
            st.save()
        try:
            fp_after, tests_after = snapshot(root, cfg)
        except Exception as exc:
            st.log({"cmd": cfg["test_cmd"], "exit": code, "seconds": round(secs, 1), "fingerprint": fp,
                    "attempt": attempt, "tests_touched": touched, "decision": "unverified",
                    "note": f"post-run check failed: {exc}"})
            raise
        touched = touched_now(tests_after)             # edits made during the run count, whatever the exit code
        changed_during = fp_after != fp
        if code != 0:
            st.forget_green(start_fp)                  # the state this run started from failed
            if changed_during:
                st.forget_green(fp_after)              # and the tree it left behind is not verified either
            tests = tests_after
            break
        if not changed_during:
            break
        st.log({"cmd": cfg["test_cmd"], "exit": code, "seconds": round(secs, 1), "fingerprint": fp,
                "attempt": attempt, "tests_touched": touched, "decision": "discarded",
                "note": "relevant files changed during this run; result not used" +
                        ("; running once more" if attempt == 1 else "")})
        fp, tests = fp_after, tests_after             # verify the tree as it is now
    if not ran:
        st.save()
        st.log({"cmd": cfg["test_cmd"], "fingerprint": fp, "tests_touched": touched, "decision": "unverified",
                "note": "no time left to start a test run"})
        msg = "Prove-It gate: NOT verified: no time to run the tests within the hook's time limit." + edit_note(touched)
        return {"systemMessage": msg} if not manual else {"decision": "block", "reason": msg}
    failed = re.findall(r"^(?:FAILED|ERROR) (\S+)", out, re.M)
    base = {"cmd": cfg["test_cmd"], "exit": code, "seconds": round(secs, 1), "fingerprint": start_fp,
            "tests_touched": touched,
            "tests_hash": hashlib.sha256(json.dumps(tests, sort_keys=True).encode()).hexdigest()[:16]}
    note = edit_note(touched)

    if code == 0 and (changed_during or no_time):
        st.save()
        why = ("files changed during the test run and there was no time for a rerun" if no_time
               else "files changed during the test run (twice)")
        if no_time:  # the attempt itself is already logged as "discarded"; this record is the decision only
            st.log({"cmd": cfg["test_cmd"], "fingerprint": fp, "tests_touched": touched, "decision": "unverified",
                    "note": why})
        msg = ("Prove-It gate: NOT verified: no time for a rerun (files changed during the test run)." if no_time
               else "Prove-It gate: NOT verified: files changed during the test run.") + note
        return {"systemMessage": msg} if not manual else {"decision": "block", "reason": msg}
    if code == 0:
        st.mark_green(fp)
    else:
        st.forget_green(start_fp)
    accepted = st.save()
    if code == 0 and not accepted:
        st.log({**base, "decision": "unverified", "note": "a newer run of the same state failed during this run"})
        msg = "Prove-It gate: NOT verified: the state changed during the run (a newer run of it failed)." + note
        return {"systemMessage": msg} if not manual else {"decision": "block", "reason": msg}
    if manual:
        st.log({**base, "decision": "check", "failed": failed,
                "note": f"manual check: {'green' if code == 0 else f'red (exit {code})'} ({secs:.1f}s)"})
        if code == 0:
            return {"systemMessage": note.strip()} if note else {}
        return {"decision": "block", "reason": failure_summary(out) + ("\n" + note.strip() if note else "")}
    if code == 5 and is_direct_pytest(cfg["test_cmd"]):
        st.s["blocks"] = 0
        st.save()
        st.log({**base, "decision": "skip", "note": "pytest collected no tests: nothing was verified"})
        return {"systemMessage": "Prove-It gate: pytest collected no tests, so this change is NOT verified." + note}
    if code == 0:
        st.s["blocks"] = 0
        st.save()
        st.log({**base, "decision": "pass", "note": f"tests green ({secs:.1f}s)"})
        return {"systemMessage": "Prove-It gate: tests pass." + note} if note else {}
    if st.s.get("blocks", 0) >= MAX_CYCLES:
        st.s["blocks"] = 0
        st.save()
        st.log({**base, "decision": "gave_up", "failed": failed,
                "note": f"still red after {MAX_CYCLES} fix cycles; stop allowed with a warning"})
        return {"systemMessage": f"Prove-It gate: tests are still failing after {MAX_CYCLES} fix cycles "
                                 f"({', '.join(failed[:3]) or 'see .prove-it/gate-log.md'}). This work is NOT verified."
                                 + note}
    st.s["blocks"] = st.s.get("blocks", 0) + 1
    st.save()
    st.log({**base, "decision": "block", "failed": failed,
            "note": f"cycle {st.s['blocks']}/{MAX_CYCLES}: tests red, agent sent back to fix"})
    warn = ""
    if touched:
        warn = ("\nTest files changed in this session (" + ", ".join(touched[:5]) + "). Do not weaken, skip or "
                "delete tests to get green; if a test is wrong, say so to the user instead.")
    head = (f"The test run timed out after {secs:.0f}s." if code == 124 else f"Tests fail (exit {code}).")
    return {"decision": "block", "reason": (
        f"Prove-It gate: you cannot finish yet. {head} Command: `{cfg['test_cmd']}`.\n"
        f"Fix the code so the tests pass (fix cycle {st.s['blocks']} of {MAX_CYCLES}), then finish. "
        f"Do not claim the task is done while tests fail.{warn}\n\n{failure_summary(out)}")}


def main(argv: list[str]) -> int:
    manual = "--check" in argv
    if os.environ.get("PROVE_IT_GATE_DISABLE", "").lower() in {"1", "true", "yes", "on"}:
        if not manual:
            return 0
        print("Prove-It: the gate hook is disabled (PROVE_IT_GATE_DISABLE); --check runs the tests anyway.",
              file=sys.stderr)
    payload: dict = {}
    if not manual:
        try:
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
        except ValueError:
            payload = {}
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd())
    session = "manual" if manual else str(payload.get("session_id", "unknown"))
    try:
        if "--baseline" in argv:
            record_baseline(root, session)
            return 0
        result = decide(root, session, manual)
    except Terminated:
        return 143
    except Exception as exc:  # a broken gate must never look like a green one
        what = f"config error: {exc}" if isinstance(exc, ConfigError) else f"{type(exc).__name__}: {exc}"
        try:
            sd = StateDir(root)
            try:
                sd.append("gate-errors.log", f"{now()} {what}\n")
            finally:
                sd.close()
        except Exception:
            pass
        if "--baseline" in argv:
            return 0  # SessionStart output would be injected; without a baseline the Stop hook runs the tests
        if manual:
            print(f"Prove-It gate ERROR: {what}", file=sys.stderr)
            return 2
        print(json.dumps({"systemMessage": f"Prove-It gate could not run ({what}). This change is NOT verified."}))
        return 0
    if manual:
        print(json.dumps(result, indent=1) if result else '{"decision": "allow"}')
        return 1 if result.get("decision") == "block" else 0
    if result:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
