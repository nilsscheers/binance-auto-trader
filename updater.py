"""
updater.py  --  Auto-update from GitHub
=========================================
Checks GitHub for a newer version of auto_trader.py on startup.
If a newer version exists:
  1. Downloads it
  2. Replaces the current file
  3. Restarts the bot automatically

Version is tracked via a VERSION file in the repo.
Safe to use -- config.py is never touched by updates.

Usage (called automatically by auto_trader.py):
    from updater import check_for_updates
    check_for_updates()
"""

import os
import sys
import time
import logging
import requests
import subprocess

log = logging.getLogger("updater")

# Files that are NEVER overwritten by updates (your personal settings)
PROTECTED_FILES = {"config.py", "updater.py"}

# GitHub raw content base URL
def _raw_url(user, repo, branch, path):
    return f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}"


def _get_remote_version(user, repo, branch) -> str:
    """Fetch the version string from GitHub VERSION file."""
    try:
        url = _raw_url(user, repo, branch, "VERSION")
        r   = requests.get(url, timeout=10)
        if r.status_code == 200:
            return r.text.strip()
    except Exception as e:
        log.debug(f"Could not fetch remote version: {e}")
    return None


def _get_local_version() -> str:
    """Read local VERSION file, return '0.0.0' if not found."""
    try:
        if os.path.isfile("VERSION"):
            with open("VERSION", "r") as f:
                return f.read().strip()
    except Exception:
        pass
    return "0.0.0"


def _version_tuple(v: str):
    """Convert '1.2.3' to (1, 2, 3) for comparison."""
    try:
        return tuple(int(x) for x in v.split("."))
    except Exception:
        return (0, 0, 0)


def _download_file(user, repo, branch, filename) -> bool:
    """Download a file from GitHub and save it locally."""
    url = _raw_url(user, repo, branch, filename)
    try:
        r = requests.get(url, timeout=30)
        if r.status_code == 200:
            # Write to temp file first, then rename (atomic)
            tmp = filename + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(r.text)
            os.replace(tmp, filename)
            log.info(f"  Downloaded: {filename}")
            return True
        else:
            log.warning(f"  Failed to download {filename}: HTTP {r.status_code}")
            return False
    except Exception as e:
        log.warning(f"  Failed to download {filename}: {e}")
        return False


def _get_file_list(user, repo, branch) -> list:
    """
    Fetch list of .py files to update from GitHub repo.
    Uses the GitHub API to list repo contents.
    Returns list of filenames.
    """
    try:
        api_url = f"https://api.github.com/repos/{user}/{repo}/contents?ref={branch}"
        r = requests.get(api_url, timeout=10, headers={"Accept": "application/vnd.github.v3+json"})
        if r.status_code == 200:
            files = [
                item["name"] for item in r.json()
                if item["type"] == "file"
                and item["name"] not in PROTECTED_FILES
                and (item["name"].endswith(".py") or item["name"] == "VERSION")
            ]
            return files
    except Exception as e:
        log.debug(f"Could not fetch file list: {e}")
    # Fallback -- just update the main script and VERSION
    return ["auto_trader.py", "VERSION"]


def check_for_updates(user: str, repo: str, branch: str = "main") -> bool:
    """
    Main entry point. Call this at bot startup.

    Returns True if an update was applied (bot will restart).
    Returns False if already up to date or update failed.
    """
    if not user or user == "YOUR_GITHUB_USERNAME":
        log.info("Auto-update: GitHub repo not configured in config.py -- skipping")
        return False

    log.info(f"Checking for updates from github.com/{user}/{repo} ...")

    local_ver  = _get_local_version()
    remote_ver = _get_remote_version(user, repo, branch)

    if remote_ver is None:
        log.warning("Could not reach GitHub -- skipping update check")
        return False

    log.info(f"  Local version:  {local_ver}")
    log.info(f"  Remote version: {remote_ver}")

    if _version_tuple(remote_ver) <= _version_tuple(local_ver):
        log.info("  Already up to date.")
        return False

    # New version available
    log.info(f"  Update available: {local_ver} -> {remote_ver}")
    log.info("  Downloading update ...")

    files_to_update = _get_file_list(user, repo, branch)
    log.info(f"  Files to update: {files_to_update}")

    success = True
    for filename in files_to_update:
        if filename in PROTECTED_FILES:
            log.info(f"  Skipping protected file: {filename}")
            continue
        if not _download_file(user, repo, branch, filename):
            success = False

    if not success:
        log.warning("  Some files failed to download -- aborting update")
        return False

    log.info(f"  Update complete: {local_ver} -> {remote_ver}")
    log.info("  Restarting bot ...")

    # Small delay so logs flush
    time.sleep(1)

    # Restart the bot by re-executing the same command
    python = sys.executable
    os.execv(python, [python] + sys.argv)

    # execv replaces the process -- we never reach here
    return True
