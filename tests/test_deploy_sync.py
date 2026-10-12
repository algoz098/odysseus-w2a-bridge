#!/usr/bin/env python3
"""Repo → container drift check (P1).

The core is **not** bind-mounted: ``src/`` is baked into the image and the
deploy is ``docker cp`` + ``docker compose restart``. Nothing in the stack
complains when that is skipped. Twice this session the container ran code
that had been fixed hours earlier — once because a ``restart`` was mistaken
for a deploy, once because an extension patch existed only in the repo.

So the drift check is the missing protection, and it has to cover both hops
that exist:

1. **core checkout → container**  (``~/Documents/odysseus`` → ``/app``)
2. **w2a → core checkout** (``~/Documents/odysseus-w2a/extensions`` →
   ``~/Documents/odysseus/extensions``) — the extension exists in two places
   and has silently diverged before.
3. **bundled → installed** (``odysseus/extensions`` → ``odysseus/data/extensions``)

Only files this workstream actually touches are checked. That is
deliberate: checking all of ``src/`` would report drift from every unrelated
upstream merge and train the reader to ignore the output, which is how a
stale container survives in the first place.

Usage::

    python3 tests/test_deploy_sync.py            # report only
    python3 tests/test_deploy_sync.py --deploy   # copy, then re-verify

Exit code is non-zero on drift so it can gate a deploy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys

W2A_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_ROOT = os.path.join(os.path.dirname(W2A_ROOT), "odysseus")
CONTAINER = os.environ.get("ODYSSEUS_CONTAINER", "odysseus-odysseus-1")
CONTAINER_ROOT = "/app"

# Core files this workstream changed (C-1..C-8 in docs/HANDOFF.md).
CORE_FILES = [
    "src/agent_loop.py",
    "src/llm_core.py",
    "src/task_scheduler.py",
    "src/mcp_manager.py",
    "src/agent_runtime/completion.py",
    "core/database.py",
]

# The extension's four deployable files.
EXT_FILES = [
    "plugin.py",
    "extension.json",
    "ui/index.html",
    "ui/script.js",
]

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _sha256(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _manifest_hashes(ext_dir: str) -> dict[str, str]:
    """``extension.json``'s ``file_hashes``, normalised to bare hex."""
    try:
        with open(os.path.join(ext_dir, "extension.json")) as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        rel: str(digest).split(":", 1)[-1]
        for rel, digest in (manifest.get("file_hashes") or {}).items()
    }


def _rehash(ext_dir: str) -> int:
    """Rewrite ``file_hashes`` from what is actually on disk.

    The manifest format is ``sha256:<hex>``. Recomputing it is easy to get
    subtly wrong (bare hex silently fails schema validation, which reads as
    "the extension is broken"), so it lives here rather than in a shell
    one-liner someone has to remember.
    """
    path = os.path.join(ext_dir, "extension.json")
    with open(path) as fh:
        manifest = json.load(fh)
    changed = 0
    for rel in list(manifest.get("file_hashes") or {}):
        actual = _sha256(os.path.join(ext_dir, rel))
        if actual is None:
            continue
        want = f"sha256:{actual}"
        if manifest["file_hashes"][rel] != want:
            manifest["file_hashes"][rel] = want
            changed += 1
    if changed:
        with open(path, "w") as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")
    return changed


def _check_manifest_hashes(ext_dir: str, label: str, failures: list[str]) -> None:
    declared = _manifest_hashes(ext_dir)
    if not declared:
        print(f"  {RED}MISSING{RESET} {label}: extension.json has no file_hashes")
        failures.append(f"{label}: no file_hashes")
        return
    for rel, want in sorted(declared.items()):
        actual = _sha256(os.path.join(ext_dir, rel))
        if actual is None:
            print(f"  {RED}MISSING{RESET} {label}/{rel}")
            failures.append(f"{label}/{rel} (file missing)")
        elif actual != want:
            print(f"  {RED}DRIFT{RESET} {label}/{rel} manifest hash is stale")
            failures.append(f"{label}/{rel} (stale manifest hash)")
        else:
            print(f"  {GREEN}ok{RESET}    {label}/{rel}")


def _container_hashes(paths: list[str]) -> dict[str, str | None]:
    """sha256 of each path inside the container, in one round trip.

    Reads through the container's own filesystem — never its SQLite (see
    C-7: WAL over a FUSE bind mount truncates committed writes).
    """
    script = (
        'for p in "$@"; do '
        'if [ -f "$p" ]; then sha256sum "$p" | cut -d" " -f1; '
        'else echo "__MISSING__"; fi; done'
    )
    try:
        out = subprocess.run(
            ["docker", "exec", CONTAINER, "sh", "-c", script, "sh", *paths],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.split()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError) as exc:
        raise SystemExit(f"{YELLOW}cannot read from container: {exc}{RESET}")
    result: dict[str, str | None] = {}
    for path, digest in zip(paths, out):
        result[path] = None if digest == "__MISSING__" else digest
    return result


def _container_running() -> bool:
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
            capture_output=True, text=True, timeout=20,
        )
        return out.returncode == 0 and out.stdout.strip() == "true"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _copy(local: str, remote: str) -> bool:
    r = subprocess.run(
        ["docker", "cp", local, f"{CONTAINER}:{remote}"],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        print(f"{RED}cp failed{RESET} {local} -> {remote}: {r.stderr.strip()}")
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deploy", action="store_true",
                    help="copy drifted files into the container and restart it")
    ap.add_argument("--rehash", action="store_true",
                    help="recompute extension.json file_hashes from disk first")
    args = ap.parse_args()

    if not os.path.isdir(CORE_ROOT):
        raise SystemExit(f"{YELLOW}core checkout not found at {CORE_ROOT}{RESET}")

    failures: list[str] = []
    ext_src = os.path.join(W2A_ROOT, "extensions", "whats2api_bridge")
    ext_dst = os.path.join(CORE_ROOT, "extensions", "whats2api_bridge")

    # ── Manifest hashes ──────────────────────────────────────────────────
    # Checked before the copy hops, because a stale ``file_hashes`` entry
    # makes OXS refuse to load the extension at all — which looks exactly
    # like A-2 (the AST audit killing every ``ext-api`` route) and cost the
    # previous session real time.
    if args.rehash:
        for ext_dir, label in ((ext_src, "w2a"), (ext_dst, "core")):
            n = _rehash(ext_dir)
            if n:
                print(f"{DIM}rehashed {n} file(s) in {label}{RESET}")
    _check_manifest_hashes(ext_src, "w2a", failures)
    _check_manifest_hashes(ext_dst, "core", failures)

    # ── Hop 2: w2a → core checkout ───────────────────────────────────────
    print(f"{DIM}extension: w2a checkout → core checkout{RESET}")
    for rel in EXT_FILES:
        a, b = _sha256(os.path.join(ext_src, rel)), _sha256(os.path.join(ext_dst, rel))
        if a is None or b is None or a != b:
            failures.append(f"extension/{rel}")
            print(f"  {RED}DRIFT{RESET} {rel}")
            if args.deploy:
                import shutil
                os.makedirs(os.path.dirname(os.path.join(ext_dst, rel)), exist_ok=True)
                shutil.copy2(os.path.join(ext_src, rel), os.path.join(ext_dst, rel))
                print(f"  {GREEN}copied{RESET} {rel}")
        else:
            print(f"  {GREEN}ok{RESET}    {rel}")

    # ── Hop 3: bundled → installed copy ──────────────────────────────────
    # The third copy. Discovery prefers bundled over data/, so this one does
    # not decide what runs, but it is where each extension's ``data_path``
    # lives and what a reinstall diffs against. It sat at 9/out, pre-analyzer,
    # for the whole session: not wrong, just quietly irrelevant until the day
    # someone reinstalls from it.
    print(f"{DIM}extension: bundled → installed copy{RESET}")
    installed = os.path.join(
        os.path.dirname(CORE_ROOT), "odysseus", "data", "extensions",
        "whats2api_bridge",
    )
    if not os.path.isdir(installed):
        print(f"  {YELLOW}skip{RESET}  no installed copy at {installed}")
    else:
        for rel in EXT_FILES:
            mine = _sha256(os.path.join(ext_dst, rel))
            theirs = _sha256(os.path.join(installed, rel))
            if mine is None or theirs is None or mine != theirs:
                failures.append(f"installed/{rel}")
                print(f"  {RED}DRIFT{RESET} {rel}")
                if args.deploy:
                    import shutil
                    os.makedirs(os.path.dirname(os.path.join(installed, rel)),
                                exist_ok=True)
                    shutil.copy2(os.path.join(ext_dst, rel),
                                 os.path.join(installed, rel))
                    print(f"  {GREEN}copied{RESET} {rel}")
            else:
                print(f"  {GREEN}ok{RESET}    {rel}")

    # ── Hop 1: core checkout → container ────────────────────────────────
    if not _container_running():
        print(f"{YELLOW}\ncontainer {CONTAINER} is not running — "
              f"skipped the repo→container hop{RESET}")
        return 1 if failures else 0

    print(f"{DIM}core: checkout → {CONTAINER}{RESET}")
    remote_paths = [f"{CONTAINER_ROOT}/{rel}" for rel in CORE_FILES]
    container_digests = _container_hashes(remote_paths)
    core_drift: list[tuple[str, str]] = []
    for rel, remote in zip(CORE_FILES, remote_paths):
        local = _sha256(os.path.join(CORE_ROOT, rel))
        there = container_digests.get(remote)
        if local is None:
            failures.append(f"core/{rel} (missing in checkout)")
            print(f"  {RED}MISSING{RESET} {rel} not in the core checkout")
        elif there is None:
            failures.append(f"core/{rel} (missing in container)")
            print(f"  {RED}MISSING{RESET} {rel} not in the container")
        elif local != there:
            failures.append(f"core/{rel}")
            core_drift.append((rel, remote))
            print(f"  {RED}DRIFT{RESET} {rel}")
        else:
            print(f"  {GREEN}ok{RESET}    {rel}")

    if args.deploy:
        for rel, remote in core_drift:
            _copy(os.path.join(CORE_ROOT, rel), remote)
        # The extension the container actually loads.
        for rel in EXT_FILES:
            _copy(os.path.join(ext_dst, rel),
                  f"{CONTAINER_ROOT}/extensions/whats2api_bridge/{rel}")
        subprocess.run(
            ["docker", "exec", CONTAINER,
             "rm", "-rf", "/app/extensions/whats2api_bridge/__pycache__"],
            capture_output=True,
        )
        print(f"{DIM}\nrestarting {CONTAINER}…{RESET}")
        subprocess.run(
            ["docker", "compose", "-f",
             os.path.join(CORE_ROOT, "docker-compose.yml"), "restart", "odysseus"],
            check=False,
        )

    if failures:
        print(f"\n{RED}{len(failures)} file(s) drifted{RESET}: "
              f"{', '.join(failures)}")
        if not args.deploy:
            print(f"{DIM}re-run with --deploy to copy and restart{RESET}")
        return 1

    print(f"\n{GREEN}container matches the checkout.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())