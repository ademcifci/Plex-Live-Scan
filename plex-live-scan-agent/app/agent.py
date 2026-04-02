#!/usr/bin/env python3
"""
Plex Live Scan - Agent
Runs on the media NAS. Watches local folders with inotify and sends
webhook notifications to the receiver on the Plex NAS.

Watch paths are auto-discovered from the container's volume mounts —
no need to list them manually in config.yaml.
"""

import os
import sys
import time
import logging
import yaml
import requests
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("plex-agent")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")

# Paths that are internal to the container — never watch these
INTERNAL_MOUNTS = {"/app", "/config", "/data", "/proc", "/sys", "/dev", "/run", "/tmp"}


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def decode_mount_path(path):
    """
    /proc/mounts encodes special characters as octal escapes, e.g. spaces
    become \\040. Decode these back to real characters so os.path.isdir()
    and watchdog can find the actual paths.
    """
    import re
    return re.sub(r'\\(\d{3})', lambda m: chr(int(m.group(1), 8)), path)


def discover_watch_paths():
    """
    Read /proc/mounts and return every bind-mounted path that looks like
    a media volume (i.e. starts with /volume) and isn't an internal container path.
    These correspond exactly to the volumes listed in docker-compose.yml.
    """
    paths = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                # Decode octal escapes (\040 → space, etc.) before any path checks
                mount_point = decode_mount_path(parts[1])
                # Only care about /volumeN/... style paths
                if not mount_point.startswith("/volume"):
                    continue
                # Skip anything that's an internal container path
                if any(mount_point.startswith(internal) for internal in INTERNAL_MOUNTS):
                    continue
                # Skip duplicate entries (e.g. overlay mounts)
                if mount_point in paths:
                    continue
                paths.append(mount_point)
    except Exception as e:
        log.error("Could not read /proc/mounts: %s", e)
    return sorted(paths)


def announce_paths(base_url, secret, paths):
    """
    Tell the receiver which paths this agent is watching.
    Called on startup and periodically so the receiver's UI stays current.
    """
    try:
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["X-Webhook-Secret"] = secret
        resp = requests.post(
            base_url + "/api/agent/paths",
            json={"paths": paths},
            headers=headers,
            timeout=10
        )
        resp.raise_for_status()
        log.info("Announced %d watch path(s) to receiver", len(paths))
    except Exception as e:
        log.warning("Could not announce paths to receiver (will retry): %s", e)


class PlexNotifier(FileSystemEventHandler):
    def __init__(self, watch_path, webhook_url, secret, debounce_seconds, ignore_patterns):
        super().__init__()
        self.watch_path = watch_path.rstrip("/")
        self.webhook_url = webhook_url
        self.secret = secret
        self.debounce = debounce_seconds
        self.ignore_patterns = ignore_patterns or []
        self._pending = {}   # path → last notification time

    def _should_ignore(self, path):
        name = os.path.basename(path)
        for pattern in self.ignore_patterns:
            if name.startswith(pattern) or name.endswith(pattern):
                return True
        return False

    def _notify(self, path):
        # Notify at the top-level changed subfolder for a targeted Plex scan
        rel = os.path.relpath(path, self.watch_path)
        parts = Path(rel).parts
        notify_path = self.watch_path if not parts else os.path.join(self.watch_path, parts[0])

        # Re-check ignore patterns against every component of the collapsed path.
        # This catches cases like @eaDir where a file deep inside passes the first
        # check (its own basename) but the parent folder should be ignored.
        for part in Path(notify_path).parts:
            if self._should_ignore(part):
                log.debug("Ignoring (matched pattern in path): %s", notify_path)
                return

        now = time.time()
        if now - self._pending.get(notify_path, 0) < self.debounce:
            return  # debounced
        self._pending[notify_path] = now

        log.info("Change detected: %s → notifying receiver", notify_path)
        try:
            headers = {"Content-Type": "application/json"}
            if self.secret:
                headers["X-Webhook-Secret"] = self.secret
            resp = requests.post(
                self.webhook_url,
                json={"path": notify_path},
                headers=headers,
                timeout=10
            )
            resp.raise_for_status()
            log.info("Webhook sent OK (HTTP %s)", resp.status_code)
        except Exception as e:
            log.error("Webhook failed: %s", e)

    def on_created(self, event):
        if not self._should_ignore(event.src_path):
            self._notify(event.src_path)

    def on_moved(self, event):
        if not self._should_ignore(event.dest_path):
            self._notify(event.dest_path)

    def on_deleted(self, event):
        if not self._should_ignore(event.src_path):
            self._notify(event.src_path)


def main():
    log.info("Plex Live Scan Agent starting…")

    if not os.path.exists(CONFIG_PATH):
        log.error("Config not found at %s", CONFIG_PATH)
        sys.exit(1)

    cfg = load_config()
    receiver    = cfg.get("receiver", {})
    webhook_url = receiver.get("url", "").rstrip("/") + "/webhook"
    secret      = receiver.get("secret", "")
    debounce    = cfg.get("debounce_seconds", 5)
    ignore      = cfg.get("ignore_patterns", [".tmp", ".part", "~", ".ds_store"])

    watches = discover_watch_paths()

    if not watches:
        log.error("No media volume mounts found. Check your docker-compose.yml volumes.")
        sys.exit(1)

    log.info("Auto-discovered %d watch path(s):", len(watches))
    for p in watches:
        log.info("  → %s", p)

    observer = Observer()
    for path in watches:
        if not os.path.isdir(path):
            log.warning("Watch path does not exist, skipping: %s", path)
            continue
        handler = PlexNotifier(path, webhook_url, secret, debounce, ignore)
        observer.schedule(handler, path, recursive=True)

    receiver_base = receiver.get("url", "").rstrip("/")

    observer.start()
    log.info("Agent running. Webhook target: %s", webhook_url)

    # Announce discovered paths so the receiver UI can show them
    announce_paths(receiver_base, secret, watches)
    last_announce = time.time()
    ANNOUNCE_INTERVAL = 1800  # re-announce every 30 minutes

    try:
        while True:
            time.sleep(5)
            if not observer.is_alive():
                log.error("Observer died, restarting…")
                observer.start()
            if time.time() - last_announce >= ANNOUNCE_INTERVAL:
                announce_paths(receiver_base, secret, watches)
                last_announce = time.time()
    except KeyboardInterrupt:
        log.info("Shutting down…")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
