#!/usr/bin/env python3
"""Flash ZMK firmware to both halves of the Sofle.

Downloads the latest GitHub Actions `firmware` artifact for this repo and
walks you through flashing each half. Double-tap the reset button on a half
to mount its bootloader volume; the script auto-detects the mount and copies
the chosen UF2.

Requires: gh CLI (logged in), macOS.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from tempfile import mkdtemp

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTLOADER_HINTS = ("NICENANO", "XIAO", "RPI-RP2", "FTHR")
POLL_INTERVAL = 0.5
MOUNT_TIMEOUT = 120
ARTIFACT_NAME = "firmware"
RUN_SEARCH_LIMIT = 20


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def resolve_repo() -> str | None:
    """Return owner/name from `origin` remote, so builds default to the user's fork."""
    try:
        url = run(["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"]).stdout.strip()
    except subprocess.CalledProcessError:
        return None
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def artifact_alive(repo: str | None, run_id: int) -> bool:
    args = ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/artifacts"] if repo \
        else ["gh", "api", f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/artifacts"]
    try:
        data = json.loads(run(args).stdout)
    except subprocess.CalledProcessError:
        return False
    return any(a.get("name") == ARTIFACT_NAME and not a.get("expired") for a in data.get("artifacts", []))


def latest_successful_run(branch: str | None, repo: str | None) -> dict:
    args = [
        "gh", "run", "list",
        "--workflow", "build.yml",
        "--status", "success",
        "--limit", str(RUN_SEARCH_LIMIT),
        "--json", "databaseId,headBranch,displayTitle,createdAt,url",
    ]
    if repo:
        args += ["--repo", repo]
    if branch:
        args += ["--branch", branch]
    proc = run(args)
    runs = json.loads(proc.stdout)
    if not runs:
        sys.exit(f"No successful build runs found{f' on branch {branch}' if branch else ''}.")
    for r in runs:
        if artifact_alive(repo, r["databaseId"]):
            return r
    where = f" on branch {branch}" if branch else ""
    sys.exit(
        f"Found {len(runs)} successful run(s){where} but every '{ARTIFACT_NAME}' artifact has expired.\n"
        f"Trigger a fresh build: gh workflow run build.yml --ref {branch or 'main'}"
        f"{f' --repo {repo}' if repo else ''}"
    )


def download_artifact(run_id: int, dest: Path, repo: str | None) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    print(f"  downloading firmware artifact from run {run_id}...")
    args = ["gh", "run", "download", str(run_id), "-n", ARTIFACT_NAME, "-D", str(dest)]
    if repo:
        args += ["--repo", repo]
    run(args)
    uf2s = sorted(dest.rglob("*.uf2"))
    if not uf2s:
        # Some workflows zip artifacts; unpack any zips and retry.
        for z in dest.rglob("*.zip"):
            with zipfile.ZipFile(z) as zf:
                zf.extractall(z.parent)
        uf2s = sorted(dest.rglob("*.uf2"))
    if not uf2s:
        sys.exit(f"No .uf2 files found in artifact at {dest}.")
    return dest


def list_uf2s(root: Path) -> list[Path]:
    return sorted(root.rglob("*.uf2"))


def pick_uf2(uf2s: list[Path], half: str) -> Path | None:
    print(f"\nSelect firmware for the {half.upper()} half:")
    suggested = None
    half_token = half.lower()
    for i, p in enumerate(uf2s, 1):
        marker = ""
        name = p.name.lower()
        if half_token in name and "reset" not in name:
            marker = "  <-- suggested"
            if suggested is None:
                suggested = i
        print(f"  [{i}] {p.name}{marker}")
    print("  [s] skip this half")
    prompt = f"Choice [default {suggested}]: " if suggested else "Choice: "
    while True:
        choice = input(prompt).strip().lower()
        if choice == "s":
            return None
        if not choice and suggested:
            return uf2s[suggested - 1]
        if choice.isdigit() and 1 <= int(choice) <= len(uf2s):
            return uf2s[int(choice) - 1]
        print("  invalid; try again")


def find_bootloader_volume() -> Path | None:
    volumes = Path("/Volumes")
    if not volumes.exists():
        return None
    for v in volumes.iterdir():
        upper = v.name.upper()
        if any(hint in upper for hint in BOOTLOADER_HINTS):
            # Confirm it looks like a UF2 bootloader (INFO_UF2.TXT lives at root).
            if (v / "INFO_UF2.TXT").exists() or (v / "INDEX.HTM").exists():
                return v
            return v
    return None


def wait_for_bootloader(half: str) -> Path:
    print(f"\nPut the {half.upper()} half into bootloader mode (double-tap reset).")
    print("Waiting for bootloader volume to mount...")
    deadline = time.monotonic() + MOUNT_TIMEOUT
    last = None
    while time.monotonic() < deadline:
        vol = find_bootloader_volume()
        if vol:
            if vol != last:
                print(f"  found {vol}")
            return vol
        time.sleep(POLL_INTERVAL)
    sys.exit(f"Timed out waiting for {half} bootloader.")


def wait_for_unmount(vol: Path) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not vol.exists():
            return
        time.sleep(POLL_INTERVAL)


def flash(half: str, uf2: Path) -> None:
    vol = wait_for_bootloader(half)
    target = vol / uf2.name
    print(f"  copying {uf2.name} -> {target}")
    try:
        shutil.copy(uf2, target)
    except OSError as e:
        # Bootloader often disconnects mid-copy; that's success, not failure.
        msg = str(e).lower()
        if "no space" in msg or "input/output" in msg or "device not configured" in msg:
            pass
        else:
            raise
    print("  flashed. waiting for board to reboot...")
    wait_for_unmount(vol)
    print(f"  {half.upper()} done.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--branch", help="git branch to pull build from (default: current branch, falls back to main)")
    ap.add_argument("--run-id", type=int, help="specific GitHub Actions run id")
    ap.add_argument("--from-dir", type=Path, help="skip download; use UF2s from this directory")
    ap.add_argument("--only", choices=["left", "right"], help="flash only one half")
    ap.add_argument("--repo", help="GitHub repo in OWNER/NAME form (default: parsed from origin remote)")
    args = ap.parse_args()

    repo = args.repo or resolve_repo()

    if args.from_dir:
        src = args.from_dir.expanduser().resolve()
        if not src.exists():
            sys.exit(f"--from-dir {src} does not exist")
        uf2s = list_uf2s(src)
        if not uf2s:
            sys.exit(f"No .uf2 files under {src}")
        print(f"Using {len(uf2s)} UF2 file(s) from {src}")
    else:
        if args.run_id:
            run_info = {"databaseId": args.run_id, "displayTitle": "(manual)", "headBranch": "(manual)"}
        else:
            branch = args.branch
            if not branch:
                try:
                    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
                except subprocess.CalledProcessError:
                    branch = None
            run_info = latest_successful_run(branch, repo)
            if not run_info and branch != "main":
                print(f"No successful run on {branch}; falling back to main")
                run_info = latest_successful_run("main", repo)
        print(f"Using run {run_info['databaseId']} on {run_info.get('headBranch')}{f' ({repo})' if repo else ''}: {run_info.get('displayTitle')}")
        tmp = Path(mkdtemp(prefix="zmk-flash-"))
        download_artifact(run_info["databaseId"], tmp, repo)
        uf2s = list_uf2s(tmp)
        print(f"Downloaded {len(uf2s)} UF2 file(s) to {tmp}")

    halves = ["left", "right"] if not args.only else [args.only]
    selections: dict[str, Path | None] = {}
    for half in halves:
        selections[half] = pick_uf2(uf2s, half)

    for half in halves:
        uf2 = selections[half]
        if uf2 is None:
            print(f"\nSkipping {half}.")
            continue
        print(f"\n=== Flashing {half.upper()}: {uf2.name} ===")
        flash(half, uf2)

    print("\nAll done.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nAborted.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"Command failed: {' '.join(e.cmd)}\n{e.stderr or e.stdout}")
