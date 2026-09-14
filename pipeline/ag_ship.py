#!/usr/bin/env python3
"""AnomalyGuessr daily ship + freshness watch (ticket #1508).

The game reads exactly one manifest: ``scenes/manifest.json`` in the game
repo, rebuilt into ``dist/`` and deployed to GitHub Pages by the "Deploy to
GitHub Pages" workflow on every push to ``main``. Until ticket #1508 the only
scheduled AnomalyGuessr job was the generator, which by design never ships
(see ``daily-anomalyguessr.yaml``); shipping was a hand-run command, and the
live set froze on 2026-09-11 when nobody ran it.

Two commands, both safe to run unattended:

    ag_ship.py ship  --data DIR --repo REPO [--date YYYY-MM-DD]
                     [--commit] [--push]
                     [--dm-channel ID] [--env .env]
    ag_ship.py check --manifest-url URL [--max-age-days N]
                     [--date YYYY-MM-DD]
                     [--dm-channel ID] [--env .env]

``ship`` serves the day's set from the accepted pool, oldest unshown first;
when the fresh pool cannot fill five slots the least recently shown accepted
scenes are recycled (``ag_queue.ship``, ticket #1206), so the daily never
comes up empty while an accepted scene exists. It writes the manifest and,
with ``--commit --push``, lands it on ``main`` so Pages redeploys.

``check`` is the watchdog: it fetches the live manifest and alerts when its
root ``date`` is older than ``--max-age-days`` (default 0, i.e. today). It
catches both a ship run that did not happen and a push/deploy that did not
reach the site, which a check of the local checkout alone would miss.

Alerts go to ``--dm-channel`` via the bot token in ``--env``
(``DISCORD_BOT_TOKEN``), the same path ``ag_generate.py`` uses. A failure
still exits 1, so cron logs and the dashboard show it too.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ag_queue  # noqa: E402  (sibling module, resolved via sys.path above)
import ag_verify  # noqa: E402

DEFAULT_MANIFEST_URL = "https://anomalyguessr.com/scenes/manifest.json"
# The live manifest is small JSON, but the Pages CDN can be slow on a cold
# edge; three tries with a short pause cover a transient blip without hiding
# a real outage.
FETCH_RETRIES = 3
FETCH_RETRY_DELAY = 5


def today() -> str:
    return datetime.date.today().isoformat()


def parse_manifest_date(text: str) -> str:
    """Return the root ``date`` of a manifest document.

    The live site plays the v2 daily manifest, whose ``date`` is the quiz day
    (``YYYY-MM-DD``). A document without it cannot be the manifest the site
    serves; that is a hard error rather than a silent "unknown".
    """
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"manifest is not valid JSON: {e}") from e
    date = doc.get("date") if isinstance(doc, dict) else None
    if not isinstance(date, str) or not ag_queue.valid_date(date):
        raise ValueError(f"manifest has no valid date: {date!r}")
    return date


def manifest_date(repo: Path) -> str:
    """The date of the manifest in a checkout (raises if it is missing/broken)."""
    path = Path(repo) / "scenes" / "manifest.json"
    if not path.exists():
        raise ValueError(f"no manifest at {path}")
    return parse_manifest_date(path.read_text(encoding="utf-8"))


def is_stale(shown: str, now: str, max_age_days: int = 0) -> bool:
    """Is ``shown`` older than ``max_age_days`` before ``now``?

    Days are calendar days, not 24h windows: a manifest that still carries
    yesterday's date is one day old even when the ship cron ran 30 hours ago.
    """
    return (_as_date(now) - _as_date(shown)).days > max_age_days


def _as_date(value: str) -> datetime.date:
    return datetime.date.fromisoformat(value)


def fetch_manifest(url: str, timeout: int = 30,
                   retries: int = FETCH_RETRIES,
                   retry_delay: int = FETCH_RETRY_DELAY) -> str:
    """Fetch the live manifest, retrying transient failures.

    Raises the last error; callers turn that into an alert.
    """
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "AnomalyGuessr-ship/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8")
        except (urllib.error.URLError, OSError) as e:
            last = e
            if attempt < retries:
                time.sleep(retry_delay)
    raise RuntimeError(f"could not read {url}: {last}")


def notify_discord(channel_id: str, content: str, token: str,
                   timeout: int = 30) -> bool:
    """Post a plain bot message to a Discord channel (alert path only)."""
    if not channel_id or not token:
        return False
    payload = json.dumps({"content": content[:1900]}).encode()
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=payload,
        headers={"Authorization": f"Bot {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "AnomalyGuessr-ship/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:  # noqa: BLE001 - alerting must never break the run
        return False


def alert(args, message: str) -> None:
    """Send one alert; print it either way so the log carries it."""
    print(message, file=sys.stderr)
    channel = getattr(args, "dm_channel", None)
    if not channel:
        return
    if getattr(args, "env", None):
        ag_verify.load_env(Path(args.env))
    notify_discord(channel, message, os.environ.get("DISCORD_BOT_TOKEN", ""))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def push_main(repo: Path) -> None:
    """Push the committed scenes to ``main``, rebasing on a rejection.

    The shared checkout can be seconds behind ``origin/main`` when a parallel
    kanban merge lands between the robot's commit and the push. A bare push is
    then rejected, and every later day replays the same rejected push because
    the local branch never caught up. Replaying the robot's own commit onto
    origin/main turns that race into a retry; anything else (auth, network)
    fails again and stays an alert.
    """
    try:
        _git(repo, "push", "origin", "main")
    except subprocess.CalledProcessError:
        _git(repo, "fetch", "origin", "main")
        _git(repo, "rebase", "origin/main")
        _git(repo, "push", "origin", "main")


def cmd_ship(args) -> int:
    date = args.date or today()
    data_dir = Path(args.data) if args.data else ag_queue.default_data_dir()
    repo = Path(args.repo)
    ids = [i.strip() for i in args.ids.split(",") if i.strip()] \
        if args.ids else None
    # The push is what deploys Pages, so --push implies the commit.
    commit = args.commit or args.push
    try:
        res = ag_queue.ship(data_dir, repo, date, commit=commit, push=False,
                            ids=ids)
    except Exception as e:  # noqa: BLE001 - any failure is an alert, not a crash
        alert(args, f"AnomalyGuessr: daily ship for {date} failed: {e}")
        return 1
    if not res["shipped"]:
        # Not a failure: the ship is idempotent per date. Still push, because
        # the first run may have committed and then failed at the push; a
        # same-day retry would otherwise never deliver the pending commit.
        print(f"already shipped for {date}")
        return _push_step(args, repo, date) if args.push else 0
    print(f"shipped {len(res['scenes'])} scenes for {date}: "
          + ", ".join(res["scenes"])
          + f" (fresh {len(res['scenes']) - res['recycled']}, "
            f"recycled {res['recycled']})")
    try:
        written = manifest_date(repo)
    except ValueError as e:
        alert(args, f"AnomalyGuessr: ship for {date} wrote no usable "
                    f"manifest: {e}")
        return 1
    if written != date:
        alert(args, f"AnomalyGuessr: ship for {date} left the manifest at "
                    f"{written} (expected {date})")
        return 1
    return _push_step(args, repo, date) if args.push else 0


def _push_step(args, repo: Path, date: str) -> int:
    try:
        push_main(repo)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip() or str(e)
        alert(args, f"AnomalyGuessr: the ship for {date} committed but the "
                    f"push to main failed: {detail}")
        return 1
    return 0


def cmd_check(args) -> int:
    now = args.date or today()
    try:
        text = fetch_manifest(args.manifest_url)
    except RuntimeError as e:
        alert(args, f"AnomalyGuessr: {e}; cannot tell whether the daily set "
                    f"is current.")
        return 1
    try:
        shown = parse_manifest_date(text)
    except ValueError as e:
        alert(args, f"AnomalyGuessr: the live manifest at "
                    f"{args.manifest_url} is unusable: {e}")
        return 1
    age = (_as_date(now) - _as_date(shown)).days
    if is_stale(shown, now, args.max_age_days):
        alert(args, f"AnomalyGuessr: the live manifest still says {shown} "
                    f"({age} day(s) old, today is {now}); the daily ship did "
                    f"not reach the site. {args.manifest_url}")
        return 1
    print(f"live manifest is current: {shown}")
    return 0


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="ag_ship.py",
        description="AnomalyGuessr daily ship + live-manifest watchdog "
                    "(ticket #1508).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ship = sub.add_parser("ship", help="serve today's accepted set")
    p_ship.add_argument("--data", default=None,
                        help="data dir (default: repo data/anomalyguessr)")
    p_ship.add_argument("--repo", required=True, type=Path,
                        help="game repo to write scenes/manifest.json into")
    p_ship.add_argument("--date", default="",
                        help="quiz day (default: today)")
    p_ship.add_argument("--commit", action="store_true",
                        help="commit the new scenes")
    p_ship.add_argument("--push", action="store_true",
                        help="commit and push (implies --commit; deploys Pages)")
    p_ship.add_argument("--ids", default="",
                        help="comma-separated scene ids to ship explicitly")
    p_ship.add_argument("--dm-channel", default=None,
                        help="Discord channel for failure alerts")
    p_ship.add_argument("--env", default=None, help=".env path for the token")

    p_check = sub.add_parser("check", help="alert when the live manifest is old")
    p_check.add_argument("--manifest-url", default=DEFAULT_MANIFEST_URL,
                         help=f"live manifest URL (default {DEFAULT_MANIFEST_URL})")
    p_check.add_argument("--max-age-days", type=int, default=0,
                         help="alert when the manifest is older than this "
                              "(default 0 = today)")
    p_check.add_argument("--date", default="", help="reference day (default today)")
    p_check.add_argument("--dm-channel", default=None,
                         help="Discord channel for the alert")
    p_check.add_argument("--env", default=None, help=".env path for the token")

    args = ap.parse_args(argv)
    if args.date and not ag_queue.valid_date(args.date):
        ap.error(f"invalid date: {args.date!r}")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.cmd == "ship":
        return cmd_ship(args)
    return cmd_check(args)


if __name__ == "__main__":
    sys.exit(main())
