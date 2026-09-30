#!/usr/bin/env python3
import fcntl
import json
import os
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mega import Mega


CONFIG_PATH = os.environ.get(
    "MEGABACKUP_CONFIG",
    os.environ.get("MEGA_BACKUP_CONFIG", "/etc/MegaBackup.json"),
)


def send_telegram(config, text):
    data = urlencode({
        "chat_id": config["telegram_chat_id"],
        "text": text,
    }).encode()
    request = Request(
        f"https://api.telegram.org/bot{config['telegram_bot_token']}/sendMessage",
        data=data,
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        result = json.loads(response.read())
    if not result.get("ok"):
        raise RuntimeError("Telegram API rejected the message")


def run_backup(config):
    paths = [Path(item).resolve() for item in config["backup_paths"]]
    if not paths:
        raise ValueError("backup_paths is empty")
    for path in paths:
        if not path.is_dir():
            raise ValueError(f"Not an existing directory: {path}")

    retention_days = int(config.get("retention_days", 30))
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")
    mega_timeout = int(config.get("mega_timeout_seconds", 600))
    if mega_timeout < 1:
        raise ValueError("mega_timeout_seconds must be at least 1")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_name = f"MegaBackup-{os.uname().nodename}-{timestamp}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="MegaBackup-") as temp_dir:
        archive_path = Path(temp_dir) / archive_name

        print("Creating backup archive...", flush=True)
        with tarfile.open(archive_path, "w:gz") as archive:
            for path in paths:
                archive.add(path, arcname=path.as_posix().lstrip("/"))

        print("Connecting to MEGA...", flush=True)
        mega_client = Mega()
        mega_client.timeout = mega_timeout
        mega = mega_client.login(config["mega_email"], config["mega_password"])
        folder_name = config.get("mega_folder", "MegaBackup")
        folder_id = mega.create_folder(folder_name)[folder_name]
        print("Uploading archive to MEGA...", flush=True)
        mega.upload(str(archive_path), dest=folder_id, dest_filename=archive_name)

    print("Removing expired backups...", flush=True)
    return archive_name, remove_expired_backups(mega, folder_id, retention_days)


def remove_expired_backups(mega, folder_id, retention_days):
    cutoff = int(datetime.now(timezone.utc).timestamp()) - retention_days * 86400
    removed = 0

    for file_id, node in mega.get_files().items():
        attributes = node.get("a")
        if node.get("t") != 0 or node.get("p") != folder_id:
            continue
        if not isinstance(attributes, dict):
            continue
        if not attributes.get("n", "").startswith("MegaBackup-"):
            continue
        try:
            file_timestamp = int(node["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if file_timestamp < cutoff:
            mega.destroy(file_id)
            removed += 1

    return removed


def main():
    with open(CONFIG_PATH, encoding="utf-8") as config_file:
        config = json.load(config_file)

    lock_path = Path(tempfile.gettempdir()) / f"MegaBackup-{os.getuid()}.lock"
    with open(lock_path, "w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Backup skipped: another backup is already running.", flush=True)
            return 0

        print("Backup started.", flush=True)
        try:
            archive_name, removed_count = run_backup(config)
        except Exception as error:
            print(f"Backup failed: {error}", file=sys.stderr)
            try:
                send_telegram(config, f"❌ Backup failed on {os.uname().nodename}")
            except Exception as notify_error:
                print(f"Telegram notification failed: {notify_error}", file=sys.stderr)
            return 1

        print(f"Backup uploaded: {archive_name}")
        try:
            send_telegram(
                config,
                f"✅ Backup completed: {archive_name}. "
                f"Expired backups deleted: {removed_count}.",
            )
        except (URLError, RuntimeError, KeyError) as error:
            print(f"Backup succeeded, but Telegram notification failed: {error}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
