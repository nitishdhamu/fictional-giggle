#!/usr/bin/env python3
"""
remove.py — remove GitHub contribution-graph activity ("un-green").

Pairs with green.py. Prompts for a repo and a removal mode:

  1) Make repo private   — instant: private repos contribute nothing to the
                           graph. Reversible (flip back anytime). No history loss.
  2) Delete repo         — permanent: removes repo AND every contribution it
                           ever created (the only true "purge" of GitHub's
                           contribution cache). Needs a token with the
                           delete_repo scope, or 10 seconds in the web UI.
  3) Un-attribute        — keeps the repo public but rewrites every commit's
                           author/committer to a neutral identity, so the
                           commits stop counting as yours. Force-pushes.

Usage:
  python remove.py                        # fully interactive
  python remove.py --repo nitishdhamu/fictional-giggle --mode 1
  python remove.py --repo nitishdhamu/fictional-giggle --mode 2 --token ghp_xxx
  python remove.py --repo nitishdhamu/fictional-giggle --mode 3

Notes:
  - Modes 2 and 3 are destructive/irreversible (mode 2 fully; mode 3 rewrites
    history). Local clones are untouched either way.
  - After any mode, GitHub's graph may take a few minutes to re-render.
"""

import argparse
import getpass
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

NEUTRAL_NAME = "Archive"
NEUTRAL_EMAIL = "archive@localhost"


# ---------------------------------------------------------------- github api

def get_token(provided: str = "") -> str:
    if provided:
        return provided.strip()
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True,
    ).stdout
    m = re.search(r"^password=(.+)$", out, re.M)
    if not m:
        sys.exit("ERROR: no stored GitHub credentials found (git credential fill).")
    return m.group(1).strip()


def gh_api(method: str, path: str, token: str, payload=None):
    data = None
    headers = {"Accept": "application/vnd.github+json",
               "Authorization": f"token {token}"}
    if payload is not None:
        import json
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"https://api.github.com{path}",
                                 data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
            return r.status, (body if body else "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def repo_exists(slug: str, token: str):
    code, body = gh_api("GET", f"/repos/{slug}", token)
    return code == 200


# ---------------------------------------------------------------- modes

def mode_private(slug: str, token: str) -> None:
    code, body = gh_api("PATCH", f"/repos/{slug}", token, {"private": True})
    if code != 200:
        sys.exit(f"ERROR: could not make repo private ({code}): {body[:200]}")
    print(f"OK: {slug} is now PRIVATE — its contributions no longer show on your graph.")
    print("    (flip back anytime: Settings -> General -> Danger Zone -> Change visibility)")


def mode_delete(slug: str, token: str) -> None:
    owner, name = slug.split("/", 1)
    code, body = gh_api("DELETE", f"/repos/{slug}", token)
    if code == 204:
        print(f"OK: {slug} deleted — all of its contributions are being purged from your graph.")
        return
    if code == 403:
        print("\nYour token lacks the 'delete_repo' scope (that's what GitHub's "
              "'Must have admin rights' error means here).\n")
        print("Two ways forward:")
        print(f"  a) Web UI (10 seconds): github.com/{slug} -> Settings -> "
              f"Danger Zone -> 'Delete this repository' -> type {name} to confirm.")
        print("  b) Rerun this tool with a delete-capable token:")
        print("       python remove.py --mode 2 --token <a classic PAT with delete_repo>")
        sys.exit(1)
    sys.exit(f"ERROR: delete failed ({code}): {body[:200]}")


def mode_unattribute(slug: str) -> None:
    """Rewrite all author/committer identities to a neutral one and force-push."""
    url = f"https://github.com/{slug}.git"
    src = tempfile.mkdtemp(prefix="rm-src-")
    dst = tempfile.mkdtemp(prefix="rm-dst-")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        subprocess.run(["git", "clone", "--quiet", "--bare", url, src],
                       check=True, env=env, capture_output=True, timeout=1800)
        subprocess.run(["git", "init", "--quiet", "--bare", dst], check=True)

        export = subprocess.run(["git", "-C", src, "fast-export", "--all"],
                                capture_output=True, timeout=1800)
        if export.returncode != 0:
            sys.exit("ERROR: fast-export failed:\n" + export.stderr.decode(errors="replace"))

        pat = re.compile(rb"^(author|committer) (.+ <[^>]+>) (\d+ [+-]\d{4})$", re.M)
        neutral_ident = f"{NEUTRAL_NAME} <{NEUTRAL_EMAIL}>".encode()

        def _sub(m):
            # callable replacement: no backreference/escape syntax involved
            return m.group(1) + b" " + neutral_ident + b" " + m.group(3)

        stream, n = pat.subn(_sub, export.stdout)
        if n == 0:
            print("nothing to rewrite: no author/committer identities found?")
            return

        imp = subprocess.run(["git", "-C", dst, "fast-import"],
                             input=stream, capture_output=True, timeout=1800)
        if imp.returncode != 0:
            with open("debug_stream.fi", "wb") as f:
                f.write(export.stdout)
            with open("debug_stream_modified.fi", "wb") as f:
                f.write(stream)
            sys.exit("ERROR: fast-import failed:\n" + imp.stderr.decode(errors="replace") +
                     "\n(dumped: debug_stream.fi = raw export, "
                     "debug_stream_modified.fi = after rewrite)")

        # verify before pushing
        idents = subprocess.run(
            ["git", "-C", dst, "for-each-ref", "--format=%(refname:short)", "refs/heads"],
            capture_output=True, text=True, check=True).stdout.split()
        for ref in idents:
            emails = subprocess.run(
                ["git", "-C", dst, "log", "--format=%ae|%ce", ref],
                capture_output=True, text=True, check=True).stdout.splitlines()
            bad = {e for e in emails if e.split("|")[0] != NEUTRAL_EMAIL
                   or e.split("|")[1] != NEUTRAL_EMAIL}
            if bad:
                sys.exit(f"VERIFY FAIL on {ref}: {sorted(bad)[:3]}")
            print(f"verified: {ref} -> all identities are {NEUTRAL_NAME} <{NEUTRAL_EMAIL}>")

        for ref in idents:
            p = subprocess.run(["git", "-C", dst, "push", "--force", url,
                                f"{ref}:{ref}"], capture_output=True, text=True,
                               env=env, timeout=1800)
            if p.returncode != 0:
                sys.exit("ERROR: push failed:\n" + (p.stderr or p.stdout))
        print(f"OK: {slug} rewritten — commits remain but no longer count as yours.")
        print("    GitHub's graph may take a few minutes to drop those squares.")
    finally:
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)


# ---------------------------------------------------------------- prompts + main

def prompt_repo(default: str) -> str:
    raw = input(f"Repo (owner/name) [{default}]: ").strip() or default
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", raw):
        sys.exit("ERROR: repo must look like owner/name")
    return raw


def prompt_mode() -> int:
    print("\nWhat should remove.py do?")
    print("  1) Make the repo private   (instant, reversible, history kept)")
    print("  2) Delete the repo         (permanent, purges its contributions)")
    print("  3) Un-attribute commits    (repo stays, commits stop counting as yours)")
    while True:
        raw = input("Select 1 / 2 / 3 [1]: ").strip() or "1"
        if raw in ("1", "2", "3"):
            return int(raw)
        print("  ! enter 1, 2 or 3")


def main():
    ap = argparse.ArgumentParser(description="Remove contribution-graph activity.")
    ap.add_argument("--repo", help="owner/name")
    ap.add_argument("--mode", choices=["1", "2", "3"], help="removal mode")
    ap.add_argument("--token", help="token override (used for API calls; needed "
                                    "for delete if your stored token lacks delete_repo)")
    args = ap.parse_args()

    print("=== remove.py — un-green your contribution graph ===\n")
    token = get_token(args.token or "")
    slug = args.repo or prompt_repo("nitishdhamu/fictional-giggle")
    if not repo_exists(slug, token):
        sys.exit(f"ERROR: {slug} not found (or token can't see it).")
    mode = int(args.mode) if args.mode else prompt_mode()

    if mode == 1:
        mode_private(slug, token)
    elif mode == 2:
        confirm = input(f'Type the repo name to confirm deletion of {slug}: ').strip()
        if confirm != slug.split("/", 1)[1]:
            sys.exit("aborted: confirmation did not match.")
        mode_delete(slug, token)
    else:
        mode_unattribute(slug)


if __name__ == "__main__":
    main()
