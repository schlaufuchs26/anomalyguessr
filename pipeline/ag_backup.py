#!/usr/bin/env python3
"""AnomalyGuessr data backup to a draft release in the game repo (ticket #1804).

The pipeline's state lives in ``data/anomalyguessr/`` and is untracked: the
queue JSONs (``state.json``, ``feedback.json``, ...) coordinate generator,
ship, gallery and API, and ``library/<id>/`` holds each scene's two images.
Until 2026-09-21 the nightly fuchs tarball carried the whole directory; it
left when the caches pushed that archive past GitHub's 2 GiB asset limit
(ticket #1801), so this script is the queue's backup now.

What goes in (``plan_backup``):

- every top-level ``*.json`` of the queue dir; the complete pipeline state,
  under 1 MB today; a new state file is picked up without touching this
  script,
- ``library/<id>/`` for the scenes that are neither shown nor rejected, in
  the order ``ship`` picks them (oldest added first), bounded by
  ``--max-cache-bytes`` (default 512 MiB; 71 such scenes, ~142 MB on
  2026-09-22).

What stays out, deliberately:

- scenes that already shipped: their images are committed under ``scenes/``
  in this repo, so git history carries them,
- rejected scenes, and the ``sources/``, ``audit/`` and ``traces/`` caches:
  regenerable (``ag_sources.py`` tops the source pool up) or diagnostics
  only,
- ``*.bak`` leftovers, which are stale by definition.

The archive mirrors the repo layout (``data/anomalyguessr/...``), so a
restore is ``tar -xzf <tarball> -C ~/projects/anomalyguessr``; it also carries
``BACKUP-INFO.json`` naming the date, what was included and what the cache cap
dropped.

The release is a **draft** on purpose: the queue holds the answer coordinates
of unshipped scenes, and the assets of a published release in this public repo
are world-readable. Draft assets need repository write access, which the box
has through ``GH_TOKEN`` (``--env``). Retention mirrors the fuchs backup: keep
everything from the last 7 days, then Sundays up to 30 days, then the first of
the month.

Usage::

    ag_backup.py [--data DIR] [--slug owner/repo] [--date YYYY-MM-DD]
                 [--max-cache-bytes N] [--max-asset-bytes N] [--work-dir DIR]
                 [--env .env] [--dm-channel ID] [--local]

``--local`` builds the tarball and keeps it without uploading or pruning
(manual runs, tests). Failures, and an incomplete backup because the cache cap
dropped scene images, go to ``--dm-channel``; a non-zero exit also shows in
the cron run history.
"""

import argparse
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ag_queue  # noqa: E402  (sibling module, resolved via sys.path above)
import ag_verify  # noqa: E402
from ag_ship import alert  # noqa: E402  (one alert path for the whole pipeline)

DEFAULT_SLUG = "schlaufuchs26/anomalyguessr"
TAG_PREFIX = "ag-data-"
TAG_RE = re.compile(r"^ag-data-(\d{4}-\d{2}-\d{2})$")
# The archive's inner root; mirrors where the data sits in the repo, so a
# restore is a plain extract into the clone.
ARCHIVE_ROOT = Path("data/anomalyguessr")
INFO_NAME = "BACKUP-INFO.json"
DEFAULT_CACHE_CAP = 512 * 1024 * 1024
# GitHub caps one release asset at 2 GiB; the fuchs backup guard learned that
# the hard way (ticket #1801). The cache cap keeps the tarball far below it.
DEFAULT_ASSET_LIMIT = 2 * 1024 * 1024 * 1024
KEEP_DAILY_DAYS = 7
KEEP_WEEKLY_DAYS = 30
STALE_WORK_AGE = 24 * 3600


def today() -> str:
    return datetime.date.today().isoformat()


def default_work_dir() -> Path:
    return Path(os.environ.get("AG_BACKUP_WORK_DIR")
                or Path.home() / ".ag-backup-tmp")


def queue_json_files(data_dir: Path) -> list:
    """Every top-level JSON file of the queue dir, by name.

    The glob instead of a fixed list keeps the backup complete when the
    pipeline gains a state file; ``*.bak`` leftovers never match.
    """
    return sorted((p for p in Path(data_dir).glob("*.json") if p.is_file()),
                  key=lambda p: p.name)


def pending_scenes(data_dir: Path) -> list:
    """Unshown, non-rejected scenes, in the order ``ship`` picks them.

    Ship takes the oldest accepted scenes first (``ag_queue.ship``), so the
    scenes that will be played next come first in the backup too: when the
    cache cap bites, what drops is the far end of the buffer. Unmoderated
    scenes stay in, because the gallery needs their images to show them.
    """
    state = ag_queue.load_state(data_dir)
    rejected = ag_queue.rejected_ids(data_dir)
    scenes = [e for e in state.get("scenes", {}).values()
              if isinstance(e, dict) and e.get("id") and not e.get("shown")
              and e["id"] not in rejected]
    return sorted(scenes, key=lambda e: (e.get("added") or "", e["id"]))


def dir_size(path: Path) -> int:
    """Bytes of all files under ``path`` (0 for a missing path)."""
    if not path.is_dir():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def plan_backup(data_dir: Path, cache_cap: int) -> dict:
    """Decide what the archive holds, before a byte is written.

    Pure: the same data dir and cap always produce the same plan, which is
    what makes the cap testable. Returns the JSON files, the library dirs
    that fit, what was skipped and why, and the totals.
    """
    data_dir = Path(data_dir)
    json_files = [{"name": p.name, "bytes": p.stat().st_size}
                  for p in queue_json_files(data_dir)]
    pending = pending_scenes(data_dir)
    scenes, skipped, used = [], [], 0
    for entry in pending:
        eid = entry["id"]
        size = dir_size(data_dir / "library" / eid)
        if size == 0:
            skipped.append({"id": eid, "reason": "no-images", "bytes": 0})
            continue
        if used + size > cache_cap:
            skipped.append({"id": eid, "reason": "over-cap", "bytes": size})
            continue
        scenes.append({"id": eid, "bytes": size})
        used += size
    return {
        "json_files": json_files,
        "scenes": scenes,
        "skipped": skipped,
        "cache_bytes": used,
        "json_bytes": sum(f["bytes"] for f in json_files),
        "pending": len(pending),
    }


def build_tarball(data_dir: Path, plan: dict, tarball: Path, date: str) -> int:
    """Write the archive; returns its size in bytes.

    Paths mirror the repo layout (``data/anomalyguessr/...``) so a restore is
    ``tar -xzf <tarball> -C ~/projects/anomalyguessr``. ``BACKUP-INFO.json``
    inside the archive records the day, the plan and the skips.
    """
    data_dir = Path(data_dir)
    tarball.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "w:gz") as tar:
        for entry in plan["json_files"]:
            tar.add(data_dir / entry["name"],
                    arcname=str(ARCHIVE_ROOT / entry["name"]))
        for entry in plan["scenes"]:
            eid = entry["id"]
            tar.add(data_dir / "library" / eid,
                    arcname=str(ARCHIVE_ROOT / "library" / eid))
        info = {
            "tool": "pipeline/ag_backup.py",
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "date": date,
            "source": str(data_dir),
            "json_files": plan["json_files"],
            "scenes": [e["id"] for e in plan["scenes"]],
            "skipped": plan["skipped"],
            "pending_scenes": plan["pending"],
            "cache_bytes": plan["cache_bytes"],
        }
        blob = json.dumps(info, indent=2, ensure_ascii=False).encode("utf-8")
        header = tarfile.TarInfo(INFO_NAME)
        header.size = len(blob)
        header.mtime = int(time.time())
        tar.addfile(header, io.BytesIO(blob))
    return tarball.stat().st_size


def retention_plan(releases, now: datetime.date) -> list:
    """Tags to delete, by the fuchs backup's retention rule.

    Everything from the last ``KEEP_DAILY_DAYS`` days stays, then only Sunday
    releases up to ``KEEP_WEEKLY_DAYS`` days, then only first-of-month ones.
    Ages come from the tag's own date (not the release timestamp), so the plan
    is deterministic; tags this script did not write are left alone.
    """
    doomed = []
    for tag, _created in releases:
        match = TAG_RE.match(tag)
        if not match:
            continue
        day = datetime.date.fromisoformat(match.group(1))
        age = (now - day).days
        if age <= KEEP_DAILY_DAYS:
            continue
        if age <= KEEP_WEEKLY_DAYS:
            if day.isoweekday() != 7:
                doomed.append(tag)
        elif day.day != 1:
            doomed.append(tag)
    return sorted(doomed)


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """One ``gh`` call; the token comes from the environment (``--env``)."""
    return subprocess.run(["gh", *args], check=check,
                          capture_output=True, text=True)


def list_releases(slug: str) -> list:
    out = gh("release", "list", "--limit", "200",
             "--json", "tagName,createdAt", "-R", slug).stdout
    return [(r.get("tagName", ""), r.get("createdAt", ""))
            for r in json.loads(out or "[]")]


def release_state(slug: str, tag: str) -> str:
    """``"draft"``, ``"published"`` or ``""`` for no release with that tag."""
    out = gh("release", "view", tag, "--json", "isDraft", "-R", slug,
             check=False)
    if out.returncode != 0:
        return ""
    try:
        return "draft" if json.loads(out.stdout or "{}").get("isDraft") else \
            "published"
    except json.JSONDecodeError:
        return ""


def publish_draft(tarball: Path, tag: str, slug: str, notes: str) -> None:
    """Upload the tarball as a draft release with a stable tag.

    ``--draft`` is the point of the whole design (see the module docstring).
    An existing *draft* of the same tag is reused (``--clobber`` refreshes the
    asset on a same-day re-run); a *published* one is refused rather than
    reused, because uploading there would put the queue, answers included,
    into a world-readable release of this public repo. Creating a second
    release under the same tag would leave duplicates behind, which is why the
    state is checked first.
    """
    state = release_state(slug, tag)
    if state == "published":
        raise RuntimeError(
            f"release {tag} exists and is published; refusing to upload the "
            f"queue there. Delete or unpublish it, or use another --slug")
    if state != "draft":
        gh("release", "create", tag, "--draft",
           "--title", f"AnomalyGuessr data {tag}", "--notes", notes,
           "-R", slug)
    gh("release", "upload", tag, str(tarball), "--clobber", "-R", slug)


def prune(slug: str, now: datetime.date) -> list:
    doomed = retention_plan(list_releases(slug), now)
    for tag in doomed:
        gh("release", "delete", tag, "--yes", "--cleanup-tag", "-R", slug,
           check=False)
    return doomed


def sweep_stale(work: Path, max_age: int = STALE_WORK_AGE) -> list:
    """Drop staging leftovers of killed runs (a SIGKILL skips the cleanup)."""
    now = time.time()
    removed = []
    for path in sorted(work.glob("ag-backup.*")):
        try:
            if now - path.stat().st_mtime <= max_age:
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink()
            removed.append(path)
        except OSError:
            continue
    return removed


def uploaded_bytes(slug: str, tag: str, name: str) -> int:
    """Size of the named asset on the release, or -1 when it is not there.

    Ticket #1801's failure was a release without its asset, noticed a day
    later; every run checks its own upload the same way.
    """
    out = gh("release", "view", tag, "--json", "assets", "-R", slug).stdout
    for asset in json.loads(out or "{}").get("assets", []):
        if asset.get("name") == name:
            return int(asset.get("size") or 0)
    return -1


def fail(args, message: str) -> int:
    """Alert (stderr + Discord) and return a non-zero exit for the cron."""
    alert(args, f"AnomalyGuessr: {message}")
    return 1


def cmd_backup(args) -> int:
    date = args.date or today()
    data_dir = Path(args.data) if args.data else ag_queue.default_data_dir()
    if args.env:
        ag_verify.load_env(Path(args.env))
    if not args.local and not os.environ.get("GH_TOKEN"):
        return fail(args, "no GH_TOKEN for the draft release; pass --env or "
                          "run with --local")
    try:
        plan = plan_backup(data_dir, args.max_cache_bytes)
    except (OSError, ValueError, KeyError) as e:  # noqa: BLE001 - any read failure is an alert
        return fail(args, f"could not plan the backup from {data_dir}: {e}")
    if not plan["json_files"]:
        return fail(args, f"no queue JSON under {data_dir}; refusing to "
                          f"upload an empty backup")
    tag = f"{TAG_PREFIX}{date}"
    work = Path(args.work_dir) if args.work_dir else default_work_dir()
    work.mkdir(parents=True, exist_ok=True)
    swept = sweep_stale(work)
    if swept:
        print(f"[backup] removed {len(swept)} stale staging dir(s)")
    tmp = Path(tempfile.mkdtemp(dir=work, prefix="ag-backup."))
    tarball = tmp / f"anomalyguessr-data-{date}.tar.gz"
    keep = bool(args.local)
    try:
        size = build_tarball(data_dir, plan, tarball, date)
        print(f"[backup] {tarball.name}: {size / 1e6:.1f} MB, "
              f"{len(plan['json_files'])} JSON file(s), "
              f"{len(plan['scenes'])}/{plan['pending']} pending scene(s), "
              f"{plan['cache_bytes'] / 1e6:.1f} MB of images")
        for entry in plan["skipped"]:
            print(f"[backup]   skipped {entry['id']} ({entry['reason']})")
        if size > args.max_asset_bytes:
            print(f"❌ [backup] tarball is {size / 1e6:.0f} MB, over the "
                  f"{args.max_asset_bytes / 1e6:.0f} MB release-asset limit; "
                  f"nothing uploaded", file=sys.stderr)
            return 2
        if args.local:
            print(f"[backup] --local: kept {tarball}")
            return 0
        notes = (f"Automated AnomalyGuessr data backup for {date}. Draft on "
                 f"purpose: the queue carries unshipped scenes and their "
                 f"answers. Restore: tar -xzf <asset> -C "
                 f"~/projects/anomalyguessr")
        publish_draft(tarball, tag, args.slug, notes)
        print(f"[backup] uploaded draft release {tag} to {args.slug}")
        remote = uploaded_bytes(args.slug, tag, tarball.name)
        if remote != size:
            return fail(args, f"release {tag} carries {remote} bytes for "
                              f"{tarball.name}, expected {size}; the asset is "
                              f"missing or truncated")
        for old in prune(args.slug, datetime.date.fromisoformat(date)):
            print(f"[backup] pruned old release {old}")
        over = [e for e in plan["skipped"] if e["reason"] == "over-cap"]
        if over:
            alert(args, f"AnomalyGuessr: backup {tag} left "
                        f"{len(over)} scene image(s) out (cache cap "
                        f"{args.max_cache_bytes / 1e6:.0f} MB); raise "
                        f"--max-cache-bytes or moderate the queue")
        return 0
    except (subprocess.CalledProcessError, RuntimeError) as e:
        detail = (getattr(e, "stderr", "") or getattr(e, "stdout", "")
                  or str(e)).strip()
        return fail(args, f"backup of {date} failed: {detail}")
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="ag_backup.py",
        description="Back up the AnomalyGuessr queue into a draft release "
                    "(ticket #1804).")
    ap.add_argument("--data", default=None,
                    help="queue data dir (default: repo data/anomalyguessr)")
    ap.add_argument("--slug", default=DEFAULT_SLUG,
                    help=f"GitHub repo for the release (default {DEFAULT_SLUG})")
    ap.add_argument("--date", default="",
                    help="backup day / tag suffix (default: today)")
    ap.add_argument("--max-cache-bytes", type=int, default=DEFAULT_CACHE_CAP,
                    help="cap for the scene images in the archive "
                         f"(default {DEFAULT_CACHE_CAP})")
    ap.add_argument("--max-asset-bytes", type=int, default=DEFAULT_ASSET_LIMIT,
                    help="refuse to upload a tarball above this size "
                         f"(default {DEFAULT_ASSET_LIMIT})")
    ap.add_argument("--work-dir", default=None,
                    help="staging dir (default ~/.ag-backup-tmp)")
    ap.add_argument("--env", default=None,
                    help=".env path for GH_TOKEN and the Discord bot token")
    ap.add_argument("--dm-channel", default=None,
                    help="Discord channel for failure alerts")
    ap.add_argument("--local", action="store_true",
                    help="build and keep the tarball; no upload, no pruning")
    args = ap.parse_args(argv)
    if args.date and not ag_queue.valid_date(args.date):
        ap.error(f"invalid date: {args.date!r}")
    return args


def main(argv=None) -> int:
    return cmd_backup(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
