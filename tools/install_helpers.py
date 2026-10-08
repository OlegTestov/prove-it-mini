#!/usr/bin/env python3
"""Prove-It Mini installer (called by install.sh; no path ever goes through a shell).

  install_helpers.py install   --kit DIR --target DIR [--dry-run] [--no-codex]
  install_helpers.py uninstall --kit DIR --target DIR [--dry-run]
  install_helpers.py setup     --kit DIR --target DIR [--dry-run] [--no-codex] [--session ID]   (plugin edition)
  install_helpers.py teardown  --kit DIR --target DIR [--dry-run]

The plugin edition (setup/teardown, run by /prove-it-mini:setup and /prove-it-mini:teardown) follows the same
rules but writes no gate copy and no settings hooks: the plugin's own hooks/hooks.json runs the gate, and only
in projects where setup completed.

Design: install only into a clean state, as one transaction.
- The target must be the top level of a git repository. Subfolders and folders outside git are refused.
- Every check runs and every final file content is computed before the first write: destinations and
  their parent folders (no symlinks), settings.json structure, rules markers, the git exclude file,
  .prove-it/ state files (no symlinks, nothing tracked by git).
- Install refuses if the gate file, the manifest, the kit hooks or a rules block already exists.
  Upgrade = uninstall, then install. Nothing is replaced, so nothing is backed up.
- Writes: fresh mkstemp() file in the destination folder, then os.replace(). If any write fails, or git
  does not actually ignore .prove-it/ afterwards, every file is put back to its original bytes.
- An O_EXCL lock file (.prove-it/install.lock) keeps two installs/uninstalls apart.
- The manifest is written FIRST (status "pending"), then the managed files, then the manifest again ("complete").
  It records relative paths, SHA-256 and the rules files it changes. Uninstall requires it (pending or complete),
  touches only those paths, deletes the gate only if its hash still matches, and never copies files back.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

GATE_REL = ".claude/prove-it/pytest_gate.py"
CONFIG_REL = ".claude/prove-it/config.json"
MANIFEST_REL = ".claude/prove-it/install-manifest.json"
SETTINGS_REL = ".claude/settings.json"
STATE_REL = ".prove-it"
LOCK_REL = ".prove-it/install.lock"
STATE_FILES = ("gate-state.json", "gate-state.lock", "gate.jsonl", "gate-log.md", "gate-errors.log")
RULES = ("CLAUDE.md", "AGENTS.md")
OWNED_FILES = {GATE_REL}  # the only file uninstall may ever delete (besides the manifest itself)

_GATE_CMD = 'python3 "$CLAUDE_PROJECT_DIR"/.claude/prove-it/pytest_gate.py'
KIT_HOOKS = [  # (event, hook definition); matched by exact equality, never by substring
    ("Stop", {"type": "command", "command": _GATE_CMD, "timeout": 600}),
    ("SessionStart", {"type": "command", "command": f"{_GATE_CMD} --baseline", "timeout": 30}),
]
OPEN_LINE = re.compile(r"^<!-- prove-it:begin\b[^\n]*-->[ \t]*$", re.M)
CLOSE_LINE = re.compile(r"^<!-- prove-it:end -->[ \t]*$", re.M)
ANY_MARKER = re.compile(r"prove-it:(begin|end)")


class Refused(Exception):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------- checks ----------

def check_path(root: Path, rel: str, kind: str = "file") -> Path:
    """rel: relative, no '..'; no existing component (incl. the last) may be a symlink; types must fit."""
    p = Path(rel)
    if p.is_absolute() or not rel or any(part in ("..", "") for part in p.parts):
        raise Refused(f"unsafe path {rel!r}: must be relative and must not contain '..'")
    cur = root
    for i, part in enumerate(p.parts):
        cur = cur / part
        if cur.is_symlink():
            raise Refused(f"{cur.relative_to(root)} is a symlink; the installer does not write through symlinks")
        if not cur.exists():
            break
        last = i == len(p.parts) - 1
        if not last and not cur.is_dir():
            raise Refused(f"{cur.relative_to(root)} is in the way: expected a folder")
        if last and kind == "file" and not cur.is_file():
            raise Refused(f"{rel} exists but is not a regular file")
        if last and kind == "dir" and not cur.is_dir():
            raise Refused(f"{rel} exists but is not a folder")
    return root / p


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def git_toplevel(root: Path) -> Path | None:
    r = git(root, "rev-parse", "--show-toplevel")
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 else None


def check_exclude_path(root: Path) -> Path | None:
    r = git(root, "rev-parse", "--git-path", "info/exclude")
    if r.returncode != 0:
        return None
    p = Path(r.stdout.strip())
    p = p if p.is_absolute() else root / p
    gitdir_r = git(root, "rev-parse", "--git-common-dir")
    common = Path(gitdir_r.stdout.strip())
    common = (common if common.is_absolute() else root / common).resolve()
    for q in (p, p.parent):  # info/exclude and info/ must be a regular file / a folder, never symlinks
        if q.is_symlink():
            raise Refused(f"git exclude path {q} is a symlink")
    if p.exists() and not p.is_file():
        raise Refused(f"git exclude path {p} is not a regular file")
    if p.parent.exists() and not p.parent.is_dir():
        raise Refused(f"{p.parent} is not a folder")
    if p.parent.resolve().parent != common:
        raise Refused(f"unexpected git exclude location {p}")
    return p


def load_settings(path: Path) -> dict:
    """Structure needed to add/remove our entries. Other hook fields are preserved, not judged."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise Refused(f"{SETTINGS_REL} is not valid JSON ({exc}). Comments are not allowed; Claude Code silently "
                      "ignores a settings file that does not parse")
    if not isinstance(data, dict):
        raise Refused(f"{SETTINGS_REL}: top level must be a JSON object")
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise Refused(f"{SETTINGS_REL}: 'hooks' must be an object")
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise Refused(f"{SETTINGS_REL}: hooks.{event} must be a list")
        for i, g in enumerate(groups):
            if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
                raise Refused(f"{SETTINGS_REL}: hooks.{event}[{i}] must be an object with a 'hooks' list")
            for j, h in enumerate(g["hooks"]):
                if not isinstance(h, dict):
                    raise Refused(f"{SETTINGS_REL}: hooks.{event}[{i}].hooks[{j}] must be an object")
    return data


def block_span(text: str, name: str) -> tuple[int, int] | None:
    """None = no kit block. (start, end) = one complete block. Anything else is refused."""
    if not ANY_MARKER.search(text):
        return None
    opens, closes = list(OPEN_LINE.finditer(text)), list(CLOSE_LINE.finditer(text))
    if len(opens) == 1 and len(closes) == 1 and len(ANY_MARKER.findall(text)) == 2 \
            and opens[0].start() < closes[0].start():
        end = closes[0].end()
        if end < len(text) and text[end] == "\n":
            end += 1
        return opens[0].start(), end
    raise Refused(f"{name}: the prove-it markers are incomplete or malformed. Fix or remove them by hand; "
                  "nothing was changed")


def hooks_without_kit(hooks: dict) -> tuple[dict, int]:
    removed, out = 0, {}
    for event, groups in hooks.items():
        new_groups = []
        for g in groups:
            kept = [h for h in g["hooks"] if (event, h) not in KIT_HOOKS]
            removed += len(g["hooks"]) - len(kept)
            if kept == g["hooks"]:
                new_groups.append(g)
            elif kept:
                new_groups.append({**g, "hooks": kept})
        if new_groups:
            out[event] = new_groups
    return out, removed


def exclude_lines_ok(data: bytes) -> bool:
    return any(line.strip() in (b".prove-it/", b"/.prove-it/", b".prove-it", b"/.prove-it")
               for line in data.splitlines())


# ---------- the transaction ----------

class Transaction:
    """Planned writes/removals with the original bytes kept in memory; rollback restores them."""

    def __init__(self, root: Path, dry: bool):
        self.root, self.dry = root, dry
        self.ops: list[tuple[str, Path, bytes | None, int | None, str]] = []  # (kind, path, data, mode, label)
        self.done: list[tuple[Path, bytes | None]] = []                      # (path, original bytes or None)
        self.created_dirs: list[Path] = []
        self.fault_after = int(os.environ.get("PROVE_IT_INSTALL_FAULT_AFTER", "0") or 0)  # tests only
        self.crash_after = int(os.environ.get("PROVE_IT_INSTALL_CRASH_AFTER", "0") or 0)  # tests only: hard exit

    def write(self, path: Path, data: bytes, label: str, mode: int | None = None) -> None:
        self.ops.append(("write", path, data, mode, label))

    def remove(self, path: Path, label: str) -> None:
        self.ops.append(("remove", path, None, None, label))

    def verify(self, label: str) -> None:
        self.ops.append(("verify", self.root, None, None, label))

    def mkdirs(self, path: Path) -> None:
        missing = []
        while not path.exists():
            missing.append(path)
            path = path.parent
        for d in reversed(missing):
            d.mkdir()
            self.created_dirs.append(d)

    def commit(self) -> None:
        for n, (kind, path, data, mode, label) in enumerate(self.ops, 1):
            print(("  [dry run] " if self.dry else "  ") + f"{kind} {label}")
            if self.dry:
                continue
            if self.fault_after and n > self.fault_after:
                raise OSError(f"injected failure before write {n} (PROVE_IT_INSTALL_FAULT_AFTER)")
            if self.crash_after and n > self.crash_after:
                os._exit(9)                                   # simulate a killed installer: no rollback runs
            if kind == "verify":
                verify_ignored(self.root)
                continue
            original = path.read_bytes() if path.exists() else None
            orig_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
            self.done.append((path, original, orig_mode))
            if kind == "remove":
                path.unlink()
            else:
                self.mkdirs(path.parent)
                atomic_write(path, data, mode if mode is not None else (orig_mode or 0o644))

    def rollback(self) -> None:
        for path, original, mode in reversed(self.done):
            try:
                if original is None:
                    if path.exists():
                        path.unlink()
                else:
                    atomic_write(path, original, mode or 0o644)
            except OSError as exc:
                print(f"  ROLLBACK FAILED for {path}: {exc}", file=sys.stderr)
        for d in reversed(self.created_dirs):
            try:
                d.rmdir()
            except OSError:
                pass
        self.done = []


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".prove-it-tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class Lock:
    def __init__(self, root: Path, dry: bool):
        self.root, self.dry, self.fd_path, self.made_state_dir = root, dry, root / LOCK_REL, False

    def __enter__(self):
        state = self.root / STATE_REL
        if self.fd_path.exists() or self.fd_path.is_symlink():
            raise Refused(f"{LOCK_REL} exists: another install or uninstall is running, or one crashed. "
                          f"If none is running, delete {LOCK_REL} and retry")
        if self.dry:
            return self
        if not state.exists():
            state.mkdir()
            self.made_state_dir = True
        try:
            fd = os.open(self.fd_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            raise Refused(f"{LOCK_REL} exists: another install or uninstall is running")
        with os.fdopen(fd, "w") as f:
            f.write(f"{os.getpid()}\n")
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self.dry:
            try:
                self.fd_path.unlink()
            except FileNotFoundError:
                pass
            if exc_type is not None and self.made_state_dir:
                try:
                    (self.root / STATE_REL).rmdir()  # a refused/failed install leaves no .prove-it/ behind
                except OSError:
                    pass
        return False


def preflight_paths(root: Path) -> None:
    top = git_toplevel(root)
    if top is None:
        raise Refused("not a git repository. Mini needs git (it keeps its log out of commits through git's "
                      "exclude file): run `git init` first. Nothing was changed")
    if top != root:
        raise Refused(f"install at the git top level ({top}), not in a subfolder. Nothing was changed")
    for rel in (".claude", ".claude/prove-it", STATE_REL):
        check_path(root, rel, "dir")
    for rel in (GATE_REL, CONFIG_REL, MANIFEST_REL, SETTINGS_REL, LOCK_REL, *RULES,
                *(f"{STATE_REL}/{name}" for name in STATE_FILES)):
        check_path(root, rel, "file")


def install(kit: Path, root: Path, dry: bool, codex: bool, edition: str = "mini", session: str = "") -> int:
    root = root.resolve()
    what = "Setting up the Prove-It Mini plugin in" if edition == "plugin" else "Installing Prove-It Mini into"
    print(f"{what} {root}" + (" (dry run: nothing is changed)" if dry else ""))
    preflight_paths(root)
    with Lock(root, dry):
        tx = plan_install(kit, root, dry, codex, edition)
        try:
            tx.commit()
            if not dry:
                verify_ignored(root)
        except BaseException as exc:
            tx.rollback()
            if isinstance(exc, Refused):
                raise
            raise Refused(f"install failed and was rolled back, nothing changed: {exc}")
    if edition == "plugin" and not dry:
        record_setup_baseline(kit, root, session)
    print(f"Test command: {effective_test_cmd(kit, root)} (change it in {CONFIG_REL})")
    return 0


def effective_test_cmd(kit: Path, root: Path) -> str:
    """What the gate will run here (env first, then config.json), read by the gate's own code; a kept config
    prints nothing during planning, so this line is the only place the command is always reported."""
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True                    # never leave __pycache__ in the kit or plugin folder
    try:
        spec = importlib.util.spec_from_file_location("prove_it_gate", kit / "tools" / "pytest_gate.py")
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        return gate.load_config(root)["test_cmd"]
    except Exception as exc:
        return f"unreadable ({exc})"
    finally:
        sys.dont_write_bytecode = old


def record_setup_baseline(kit: Path, root: Path, session: str = "") -> None:
    """Best effort: the gate's baseline as of setup, under "plugin-setup" (for a session whose SessionStart ran
    while the plugin was off) and, replacing any older one, under the session that ran setup (it may keep a
    baseline from before a teardown). If this fails, the gate simply runs the tests at that session's next stop."""
    for sid in ("plugin-setup", *([session] if session else [])):
        try:
            subprocess.run([sys.executable, str(kit / "tools" / "pytest_gate.py"), "--baseline", "--refresh"],
                           input=json.dumps({"session_id": sid}), text=True, capture_output=True, timeout=120,
                           env=dict(os.environ, CLAUDE_PROJECT_DIR=str(root)))
        except (OSError, subprocess.SubprocessError):
            pass


def existing_edition(root: Path) -> str:
    try:
        return str(json.loads((root / MANIFEST_REL).read_text(encoding="utf-8")).get("edition") or "mini")
    except (OSError, ValueError, AttributeError):
        return "mini"


def remove_hint(edition: str) -> str:
    return "/prove-it-mini:teardown" if edition == "plugin" else "./install.sh --uninstall"


def plan_install(kit: Path, root: Path, dry: bool, codex: bool, edition: str = "mini") -> Transaction:
    gate_src = kit / "tools" / "pytest_gate.py"
    rule_src = {name: kit / "rules" / name for name in RULES}
    for src in [gate_src, *rule_src.values()]:
        if not src.is_file():
            raise Refused(f"kit file missing: {src}")
    if (root / MANIFEST_REL).exists():
        try:
            status = json.loads((root / MANIFEST_REL).read_text(encoding="utf-8")).get("status")
        except (ValueError, AttributeError):
            status = None
        if status == "pending":
            raise Refused(f"a previous install was interrupted; run {remove_hint(existing_edition(root))} first, "
                          "then install. Nothing was changed")
    for rel in (GATE_REL, MANIFEST_REL):
        if (root / rel).exists():
            hint = remove_hint(existing_edition(root)) if (root / MANIFEST_REL).exists() else "./install.sh --uninstall"
            raise Refused(f"{rel} already exists: Prove-It is already installed here. To reinstall or switch between "
                          f"install.sh and the plugin: {hint}, then install. Nothing was changed")
    settings_path = root / SETTINGS_REL
    try:
        settings = load_settings(settings_path)
    except Refused:
        if edition != "plugin":
            raise
        settings = {}                  # setup never writes settings.json, and Claude Code ignores one that won't parse
    if hooks_without_kit(settings.get("hooks", {}))[1]:   # leftover project hooks would run the gate a second time
        raise Refused(f"{SETTINGS_REL} already contains the Prove-It hooks. Uninstall first (./install.sh "
                      "--uninstall; if that refuses, delete the two prove-it hook entries by hand). Nothing was changed")
    managed_rules = list(RULES if codex else RULES[:1])
    rules_text = {}
    for name in managed_rules:
        p = root / name
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        if block_span(text, name) is not None:
            raise Refused(f"{name} already contains a Prove-It block. Uninstall first. Nothing was changed")
        rules_text[name] = text
        if block_span(rule_src[name].read_text(encoding="utf-8"), f"kit {name}") is None:
            raise Refused(f"kit rules file {rule_src[name]} has no valid block")
    exclude = None
    if git_toplevel(root) is not None:
        tracked = [n for n in git(root, "ls-files", "-z", "--", STATE_REL).stdout.split("\0") if n]
        if tracked:
            raise Refused(f"{', '.join(tracked[:5])} is tracked by git; the gate log must never be committed. "
                          f"Remove it from git (git rm --cached) first. Nothing was changed")
        exclude = check_exclude_path(root)

    tx = Transaction(root, dry)
    gate_bytes = gate_src.read_bytes()
    plugin = edition == "plugin"                       # the plugin brings its own gate and hooks
    created_settings = not settings_path.exists() and not plugin
    manifest = {"version": 4, "edition": edition, "status": "pending",
                "files": {} if plugin else {GATE_REL: sha256_bytes(gate_bytes)},
                "created_settings": created_settings, "rules": managed_rules,
                "hooks": [] if plugin else [{"event": e, "hook": h} for e, h in KIT_HOOKS]}
    # written FIRST: if the installer is killed midway, uninstall still knows what may have been installed
    tx.write(root / MANIFEST_REL, (json.dumps(manifest, indent=1) + "\n").encode(), f"{MANIFEST_REL} (pending)", 0o644)
    if exclude is not None:
        old = exclude.read_bytes() if exclude.exists() else b""
        if not exclude_lines_ok(old):
            tx.write(exclude, old + (b"" if not old or old.endswith(b"\n") else b"\n") + b".prove-it/\n",
                     "git exclude: .prove-it/")
    if exclude is not None:
        tx.verify("check that git ignores .prove-it/ (before any gate file or hook exists)")
    if not plugin:
        tx.write(root / GATE_REL, gate_bytes, GATE_REL, 0o644)
    if not (root / CONFIG_REL).exists():
        test_cmd = "python3 -m pytest -q"
        for venv in (".venv", "venv"):
            if (root / venv / "bin" / "python").exists():
                test_cmd = f"{venv}/bin/python -m pytest -q"
                break
        tx.write(root / CONFIG_REL, (json.dumps({"test_cmd": test_cmd, "timeout": 300}, indent=2) + "\n").encode(),
                 f"{CONFIG_REL} (test command: {test_cmd})", 0o644)
    if not plugin:
        hooks = dict(settings.get("hooks", {}))
        for event, hook in KIT_HOOKS:
            hooks.setdefault(event, []).append({"hooks": [dict(hook)]})
        tx.write(settings_path, (json.dumps(dict(settings, hooks=hooks), indent=2, ensure_ascii=False) + "\n").encode(),
                 SETTINGS_REL)
    for name, text in rules_text.items():
        block = rule_src[name].read_text(encoding="utf-8").strip() + "\n"
        tx.write(root / name, ((text.rstrip("\n") + "\n\n" if text.strip() else "") + block).encode(), name)
    manifest["status"] = "complete"
    tx.write(root / MANIFEST_REL, (json.dumps(manifest, indent=1) + "\n").encode(), f"{MANIFEST_REL} (complete)", 0o644)
    return tx


def verify_ignored(root: Path) -> None:
    """After writing: git must really ignore the gate's state files (a negation elsewhere can undo it)."""
    if git_toplevel(root) is None:
        return
    for name in ("gate.jsonl", "gate-log.md", "gate-state.json"):
        r = git(root, "check-ignore", "-q", f"{STATE_REL}/{name}")
        if r.returncode != 0:
            raise Refused(f"git does not ignore {STATE_REL}/{name} even after adding '.prove-it/' to the exclude "
                          "file (a '!' rule in .gitignore probably re-includes it). Remove that rule and retry. "
                          "Everything was rolled back")


def uninstall(kit: Path, root: Path, dry: bool) -> int:
    root = root.resolve()
    print(f"Uninstalling Prove-It Mini from {root}" + (" (dry run)" if dry else ""))
    preflight_paths(root)
    with Lock(root, dry):
        tx = plan_uninstall(root, dry)
        try:
            tx.commit()
        except BaseException as exc:
            tx.rollback()
            raise Refused(f"uninstall failed and was rolled back: {exc}")
    print(f"Done. Kept: {CONFIG_REL} (your settings) and {STATE_REL}/ (gate log).")
    return 0


def plan_uninstall(root: Path, dry: bool) -> Transaction:
    manifest_path = root / MANIFEST_REL
    if not manifest_path.exists():
        raise Refused(f"no {MANIFEST_REL}: Prove-It Mini is not installed here (or the manifest was removed). "
                      "Nothing was changed; remove leftovers by hand")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise Refused(f"{MANIFEST_REL} is not valid JSON ({exc}); nothing was changed")
    files = manifest.get("files") if isinstance(manifest, dict) else None
    rules = manifest.get("rules") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
        raise Refused(f"{MANIFEST_REL}: 'files' must map paths to hashes; nothing was changed")
    if set(files) - OWNED_FILES:
        raise Refused(f"{MANIFEST_REL} lists paths the kit never installs ({', '.join(sorted(set(files) - OWNED_FILES))}); "
                      "refusing to touch them. Nothing was changed")
    if not isinstance(rules, list) or not set(rules) <= set(RULES):
        raise Refused(f"{MANIFEST_REL}: 'rules' must list only {', '.join(RULES)}; nothing was changed")
    for rel in files:
        check_path(root, rel, "file")
    settings_path = root / SETTINGS_REL
    # a plugin setup never adds settings hooks (and refuses leftover ones), so its teardown never reads the file
    settings = {} if manifest.get("edition") == "plugin" else load_settings(settings_path)
    new_hooks, removed = hooks_without_kit(settings.get("hooks", {}))
    spans = {}
    for name in rules:
        p = root / name
        if p.exists():
            text = p.read_text(encoding="utf-8")
            span = block_span(text, name)
            if span is not None:
                spans[name] = (text, span)

    tx = Transaction(root, dry)
    if manifest.get("status") == "pending":
        print("  the manifest says the install was interrupted: removing whatever of it is present")
    for rel, digest in sorted(files.items()):
        p = root / rel
        if p.exists():                                # missing is fine: an interrupted install may not have written it
            if sha256_bytes(p.read_bytes()) == digest:
                tx.remove(p, rel)
            else:
                print(f"  kept {rel}: it changed after installation (not the kit's file)")
    if removed:
        data = dict(settings)
        if new_hooks:
            data["hooks"] = new_hooks
        else:
            data.pop("hooks", None)
        if not data and manifest.get("created_settings") is True:
            tx.remove(settings_path, f"{SETTINGS_REL} (created by the installer, empty again)")
        else:
            tx.write(settings_path, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode(), SETTINGS_REL)
    for name, (text, (start, end)) in spans.items():
        rest = (text[:start].rstrip("\n") + ("\n" + text[end:].lstrip("\n") if text[end:].strip() else "")).strip("\n")
        if rest:
            tx.write(root / name, (rest + "\n").encode(), name)
        else:
            tx.remove(root / name, f"{name} (it only held the kit block)")
    tx.remove(manifest_path, MANIFEST_REL)
    return tx


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="action", required=True)
    for name in ("install", "uninstall", "setup", "teardown"):
        s = sub.add_parser(name)
        s.add_argument("--kit", required=True)
        s.add_argument("--target", required=True)
        s.add_argument("--dry-run", action="store_true")
        if name in ("install", "setup"):
            s.add_argument("--edition", default="mini", choices=["mini"])
            s.add_argument("--no-codex", action="store_true")
        if name == "setup":
            s.add_argument("--session", default="", help="session that runs setup (its baseline is refreshed)")
    a = p.parse_args(argv[1:])
    kit, target = Path(a.kit).resolve(), Path(a.target)
    if not target.is_dir():
        print(f"target directory not found: {target}", file=sys.stderr)
        return 2
    if target.resolve() == kit:
        print("install into your project, not into the kit folder", file=sys.stderr)
        return 2
    try:
        if a.action == "install":
            return install(kit, target, a.dry_run, not a.no_codex)
        if a.action == "setup":
            return install(kit, target, a.dry_run, not a.no_codex, edition="plugin", session=a.session)
        return uninstall(kit, target, a.dry_run)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
