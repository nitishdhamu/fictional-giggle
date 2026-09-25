#!/usr/bin/env python3
"""
green.py — GitHub contribution graph backfill tool.

Prompts for:
  1. Start date and end date
  2. Which days of the week to commit on (Mon..Sun individually, any combo, or ALL)
  3. Commits per day: minimum and maximum

Then rebuilds the repo history locally via `git fast-import`, verifies it
(count, date coverage, no commits on unselected weekdays), and force-pushes
it to GitHub. Commits are authored as nitishdhamu with the account's noreply
email so GitHub counts them on the contribution graph.

Usage:
  python green.py                       # fully interactive
  python green.py --start 2005-02-04 --end 2026-09-25 --days all --min 10 --max 40
  python green.py --start 2026-09-01 --end 2026-09-20 --days 1,3,5 --min 5 --max 15 --dry-run

Notes:
  - Pushes to https://github.com/nitishdhamu/fictional-giggle (change REPO below
    or pass --repo owner/name). Uses your stored git credentials; if the repo
    doesn't exist it is created automatically (public) via the API.
  - To fully reset GitHub's contribution cache for this repo, delete the repo
    in the GitHub UI first (Settings -> Danger Zone), then run this tool —
    it will recreate and re-push from scratch.
  - After pushing, GitHub's graph may take a few minutes (fresh repo) or
    longer (replacing an existing history) to re-render.
"""

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, date, timedelta, timezone

# ---------------------------------------------------------------- constants

LOGIN = "nitishdhamu"
UID = 95616314
EMAIL = f"{UID}+{LOGIN}@users.noreply.github.com"
IDENT = f"{LOGIN} <{EMAIL}>"
REPO_DEFAULT = "nitishdhamu/fictional-giggle"

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # weekday() index

def build_readme() -> bytes:
    """A red carpet README: 1,000,000+ lines of carpet runner."""
    W = 20                 # emoji per row
    ROWS = 1_000_000       # red rows (plus borders/stripes => 1M+ lines total)
    red, gold = "\U0001F7E5", "\U0001F7E8"
    red_row, gold_row = red * W, gold * W
    lines = [
        "# \U0001F3AC THE RED CARPET \U0001F3AC",
        "",
        "Roll it out. Walk it daily. Never let the graph go grey.",
        "",
        "## Tools on this carpet",
        "",
        "- `green.py` — interactive backfill: asks for start/end date, which",
        "  weekdays (1=Mon..7=Sun, 8=all), and min/max commits per day; builds",
        "  the whole history via git fast-import, verifies it, pushes.",
        "  Flags: `--start --end --days --min --max --repo --dry-run`",
        "- `remove.py` — un-green: make repo private, delete it, or rewrite",
        "  every commit to a neutral identity. Flags: `--repo --mode 1|2|3`",
        "",
        "Now walking the carpet... \U0001F9CD",
        "",
    ]
    lines += [gold_row] * 3
    for i in range(ROWS):
        lines.append(gold_row if i % 25 == 12 else red_row)
    lines += [gold_row] * 3
    lines += ["", "\U0001F3AC END OF THE CARPET \U0001F3AC", ""]
    return ("\n".join(lines) + "\n").encode("utf-8")


BLOB = (
    "# activity-log\n\n"
    "A tiny personal project used to track daily activity.\n\n"
    + "".join(f"- entry {i:04d}\n" for i in range(1, 121))
).encode("utf-8")


# ---------------------------------------------------------------- github api

def get_token() -> str:
    """Read the stored GitHub credential via git credential fill."""
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True,
    ).stdout
    m = re.search(r"^password=(.+)$", out, re.M)
    if not m:
        sys.exit("ERROR: no stored GitHub credentials found (git credential fill).")
    return m.group(1).strip()


def gh_api(method: str, path: str, payload=None, token: str = ""):
    url = f"https://api.github.com{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"token {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {}


def ensure_repo(slug: str, token: str) -> None:
    """Make sure the repo exists; create it (public) if missing."""
    code, body = gh_api("GET", f"/repos/{slug}", token=token)
    if code == 200:
        print(f"repo: {slug} exists (private={body.get('private')})")
        return
    if code == 404:
        print(f"repo: {slug} not found -> creating it (public)...")
        code, body = gh_api("POST", "/user/repos",
                            {"name": slug.split("/", 1)[1], "private": False,
                             "auto_init": False}, token)
        if code not in (201, 202):
            sys.exit(f"ERROR: could not create repo ({code}): {body}")
        print("repo: created.")
        return
    sys.exit(f"ERROR: GitHub API returned {code} for /repos/{slug}: {body}")


# ---------------------------------------------------------------- prompts

def prompt_date(label: str, default: str) -> date:
    while True:
        raw = input(f"{label} [{default}]: ").strip() or default
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            print("  ! enter a date as YYYY-MM-DD, e.g. 2005-02-04")


def prompt_days() -> set:
    print("\nWhich days should get commits?")
    for i, name in enumerate(DAY_NAMES):
        print(f"  {i + 1}) {name}")
    print("  8) ALL days")
    while True:
        raw = input("Select (e.g. 1,2,3,4,5 or 8 for all) [8]: ").strip() or "8"
        if raw.lower() in ("all", "8"):
            return set(range(7))
        try:
            picks = {int(x) for x in re.split(r"[,\s]+", raw) if x}
            if picks and picks <= set(range(1, 8)):
                return {p - 1 for p in picks}
        except ValueError:
            pass
        print("  ! enter numbers 1-7 (comma or space separated), or 8 / all")


def prompt_count(label: str, default: int, lo: int = 1, hi: int = 500) -> int:
    while True:
        raw = input(f"{label} [{default}]: ").strip() or str(default)
        try:
            v = int(raw)
            if lo <= v <= hi:
                return v
        except ValueError:
            pass
        print(f"  ! enter a number between {lo} and {hi}")


# ---------------------------------------------------------------- stream

def build_stream(start: date, end: date, allowed: set, mn: int, mx: int):
    """Emit a git fast-import stream with mn..mx commits on every allowed day."""
    now = datetime.now(timezone.utc)
    blob = build_readme()
    out = bytearray()
    out += b"blob\nmark :1\n"
    out += b"data %d\n" % len(blob)
    out += blob
    out += b"\n"

    mark = 2
    total = 0
    day_list = []

    d = start
    while d <= end:
        if d.weekday() not in allowed:
            d += timedelta(days=1)
            continue
        midnight = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
        n = random.randint(mn, mx)
        if d == now.date():
            # today: pure seconds-into-day offsets, clamped to 'now'
            avail_off = int(now.timestamp()) - 120 - midnight
            lo_off = 6 * 3600
            if avail_off - lo_off < 1800:
                lo_off = 1
            if avail_off < max(n, 2):        # just after midnight UTC
                offs = list(range(1, min(n, max(avail_off - 1, 1)) + 1))
            elif avail_off - lo_off >= n:
                offs = sorted(random.sample(range(lo_off, avail_off), n))
            else:
                step = max(1, (avail_off - lo_off) // max(n, 1))
                offs = [lo_off + i * step for i in range(n) if lo_off + i * step <= avail_off]
        else:
            offs = sorted(random.sample(range(6 * 3600, 23 * 3600), n))
        for s in offs:
            assert 0 <= s < 86400
            ts = midnight + s
            out += b"commit refs/heads/main\n"
            out += b"mark :%d\n" % mark
            out += b"author %s %d +0000\n" % (IDENT.encode(), ts)
            out += b"committer %s %d +0000\n" % (IDENT.encode(), ts)
            msg = b"update"
            out += b"data %d\n%s\n" % (len(msg), msg)
            if mark == 2:
                out += b"M 100644 :1 README.md\n"
            else:
                out += b"from :%d\n" % (mark - 1)
            out += b"\n"
            mark += 1
            total += 1
        day_list.append(d)
        d += timedelta(days=1)

    if not day_list:
        sys.exit("ERROR: no eligible days in that range with the selected weekdays.")
    return bytes(out), day_list, total


# ---------------------------------------------------------------- import + verify

def import_history(stream: bytes):
    repo = tempfile.mkdtemp(prefix="green-")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    p = subprocess.run(["git", "fast-import"], cwd=repo, input=stream,
                       capture_output=True)
    if p.returncode != 0:
        sys.exit("ERROR: fast-import failed:\n" + p.stderr.decode(errors="replace"))
    m = re.search(r"new tip ([0-9a-f]+)", p.stderr.decode(errors="replace"))
    if m:  # safety net; fresh repo should update main directly
        subprocess.run(["git", "update-ref", "refs/heads/main", m.group(1)],
                       cwd=repo, check=True)
    return repo


def verify(repo: str, start: date, end: date, allowed: set, mn: int, mx: int, expected_total: int):
    g = ["git", "-C", repo]

    days_raw = subprocess.run(g + ["log", "--format=%ad", "--date=short"],
                              capture_output=True, text=True, check=True).stdout.split()
    dates = [date.fromisoformat(x) for x in set(days_raw)]

    bad_days = sorted({d for d in dates if d.weekday() not in allowed})
    if bad_days:
        sys.exit(f"VERIFY FAIL: commits exist on unselected weekdays: {bad_days[:5]}")

    # every eligible day in the span must be present
    d, missing = start, []
    while d <= end:
        if d.weekday() in allowed and d not in dates:
            missing.append(d.isoformat())
        d += timedelta(days=1)
    if missing:
        sys.exit(f"VERIFY FAIL: missing days: {missing[:5]}")

    per = {}
    for x in days_raw:
        per[x] = per.get(x, 0) + 1
    if min(per.values()) < mn or max(per.values()) > mx:
        sys.exit(f"VERIFY FAIL: per-day counts out of range "
                 f"({min(per.values())}..{max(per.values())}, wanted {mn}..{mx})")

    count = int(subprocess.run(g + ["rev-list", "--count", "main"],
                               capture_output=True, text=True, check=True).stdout)
    if count != expected_total:
        sys.exit(f"VERIFY FAIL: commit count {count} != expected {expected_total}")

    idents = subprocess.run(g + ["log", "--format=%an <%ae> | %cn <%ce>"],
                            capture_output=True, text=True, check=True).stdout.splitlines()
    if set(idents) != {f"{IDENT} | {IDENT}"}:
        sys.exit(f"VERIFY FAIL: unexpected author identities: {set(idents)}")

    return count, min(dates), max(dates)


def push(repo: str, repo_url: str):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    p = subprocess.run(["git", "push", "--force", repo_url, "main"],
                       cwd=repo, capture_output=True, text=True, env=env, timeout=1800)
    if p.returncode != 0:
        sys.exit("ERROR: push failed:\n" + (p.stderr or p.stdout))
    print("push: OK")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Backfill GitHub contribution graph.")
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--days", help="'all' or weekday numbers 1=Mon..7=Sun, e.g. 1,2,3,4,5")
    ap.add_argument("--min", type=int); ap.add_argument("--max", type=int)
    ap.add_argument("--repo", default=REPO_DEFAULT, help="owner/name")
    ap.add_argument("--dry-run", action="store_true", help="build + verify locally, do not push")
    args = ap.parse_args()

    print("=== green.py — GitHub contribution backfill ===\n")
    start = (datetime.strptime(args.start, "%Y-%m-%d").date() if args.start
             else prompt_date("Start date (YYYY-MM-DD)", "2005-02-04"))
    end = (datetime.strptime(args.end, "%Y-%m-%d").date() if args.end
           else prompt_date("End date   (YYYY-MM-DD)", date.today().isoformat()))
    if end < start:
        sys.exit("ERROR: end date is before start date.")

    if args.days:
        a = args.days.strip().lower()
        if a in ("all", "8"):
            allowed = set(range(7))
        else:
            try:
                allowed = {int(x) - 1 for x in re.split(r"[,\s]+", a) if x}
            except ValueError:
                sys.exit("ERROR: --days must be 'all' or numbers 1-7 (e.g. 1,2,3,4,5)")
        if not allowed or not allowed <= set(range(7)):
            sys.exit("ERROR: --days must be 'all' or numbers 1-7 (e.g. 1,2,3,4,5)")
    else:
        allowed = prompt_days()

    mn = args.min if args.min else prompt_count("Minimum commits per day", 10)
    mx = args.max if args.max else prompt_count("Maximum commits per day", 40)
    if mn > mx:
        sys.exit("ERROR: minimum exceeds maximum.")

    sel = ", ".join(DAY_NAMES[i] for i in sorted(allowed))
    print(f"\nPlan: {start} -> {end} | days: {sel} | {mn}-{mx} commits/day\n")

    stream, day_list, total = build_stream(start, end, allowed, mn, mx)
    print(f"built: {total} commits across {len(day_list)} eligible days "
          f"(non-selected weekdays skipped)")

    repo = import_history(stream)
    count, first, last = verify(repo, start, end, allowed, mn, mx, total)
    print(f"verified: {count} commits | {first} -> {last} | only selected weekdays | "
          f"{mn}-{mx}/day | identity {IDENT}")

    if args.dry_run:
        print("\ndry-run: OK — nothing was pushed.")
        shutil.rmtree(repo, ignore_errors=True)
        return

    token = get_token()
    ensure_repo(args.repo, token)
    push(repo, f"https://github.com/{args.repo}.git")
    shutil.rmtree(repo, ignore_errors=True)
    print(f"\ndone: {count} commits pushed to {args.repo}.")
    print("note: GitHub's contribution graph may need a few minutes to re-render "
          "(longer if this replaced an existing history).")


if __name__ == "__main__":
    main()
