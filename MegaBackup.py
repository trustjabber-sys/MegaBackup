#!/usr/bin/env python3
import base64
import contextlib
import hashlib
import fcntl
import html
import json
import os
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mega import Mega
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout


CONFIG_PATH = os.environ.get(
    "MEGABACKUP_CONFIG",
    os.environ.get("MEGA_BACKUP_CONFIG", "/etc/MegaBackup.json"),
)
RETRYABLE_MEGA_ERRORS = (
    RequestsTimeout,
    RequestsConnectionError,
    TimeoutError,
    json.JSONDecodeError,
)
HASHCASH_REPLICATIONS = 262144
HASHCASH_SLOT_SIZE = 48
HASHCASH_SOLVE_TIMEOUT_SECONDS = 300


def send_telegram(config, text):
    data = urlencode({
        "chat_id": config["telegram_chat_id"],
        "text": text,
        "parse_mode": "HTML",
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


def solve_hashcash(challenge):
    parts = challenge.split(":")
    if len(parts) != 4 or parts[0] != "1":
        raise ValueError("Unsupported MEGA Hashcash challenge")

    try:
        easiness = int(parts[1])
        token = base64.urlsafe_b64decode(parts[3] + "=" * (-len(parts[3]) % 4))
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid MEGA Hashcash challenge") from error
    if not 0 <= easiness <= 255 or not token:
        raise ValueError("Invalid MEGA Hashcash parameters")

    token += b"\0" * (-len(token) % 16)
    if len(token) > HASHCASH_SLOT_SIZE:
        raise ValueError("MEGA Hashcash token is too long")

    threshold = (
        (((easiness & 63) << 1) + 1) << ((easiness >> 6) * 7 + 3)
    ) & 0xFFFFFFFF
    buffer = bytearray(4 + HASHCASH_REPLICATIONS * HASHCASH_SLOT_SIZE)
    for offset in range(4, len(buffer), HASHCASH_SLOT_SIZE):
        buffer[offset:offset + len(token)] = token

    deadline = time.monotonic() + HASHCASH_SOLVE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        for index in range(4):
            buffer[index] = (buffer[index] + 1) & 0xFF
            if buffer[index]:
                break
        else:
            raise RuntimeError("MEGA Hashcash counter exhausted")

        if int.from_bytes(hashlib.sha256(buffer).digest()[:4], "big") <= threshold:
            return base64.urlsafe_b64encode(buffer[:4]).decode("ascii").rstrip("=")

    raise TimeoutError("Timed out solving MEGA Hashcash challenge")


def make_hashcash_post(original_post):
    def post(url, *args, **kwargs):
        request_kwargs = dict(kwargs)
        response = original_post(url, *args, **request_kwargs)

        for _ in range(3):
            if response.status_code != 402:
                return response

            challenge = response.headers.get("X-Hashcash")
            if not challenge:
                response.close()
                raise RuntimeError("MEGA returned HTTP 402 without X-Hashcash")

            try:
                parts = challenge.split(":")
                if len(parts) != 4 or parts[0] != "1":
                    raise ValueError("Unsupported MEGA Hashcash challenge")
                proof = solve_hashcash(challenge)
            except Exception:
                response.close()
                raise

            response.close()
            headers = dict(kwargs.get("headers") or {})
            headers["X-Hashcash"] = f"1:{parts[3]}:{proof}"
            request_kwargs["headers"] = headers
            print("MEGA requested Hashcash proof; retrying API request...", flush=True)
            response = original_post(url, *args, **request_kwargs)

        if response.status_code == 402:
            response.close()
            raise RuntimeError("MEGA still returned HTTP 402 after Hashcash proof")
        return response

    return post


@contextlib.contextmanager
def enable_mega_hashcash():
    import requests

    original_post = requests.post
    requests.post = make_hashcash_post(original_post)
    try:
        yield
    finally:
        requests.post = original_post


def upload_with_retries(mega, archive_path, folder_id, archive_name, config):
    retries = int(config.get("upload_retries", 3))
    retry_delay = int(config.get("upload_retry_delay_seconds", 30))
    if retries < 0 or retry_delay < 0:
        raise ValueError("Upload retries and retry delay must not be negative")

    for attempt in range(retries + 1):
        try:
            return mega.upload(
                str(archive_path),
                dest=folder_id,
                dest_filename=archive_name,
            )
        except RETRYABLE_MEGA_ERRORS as error:
            if attempt == retries:
                raise
            delay = min(retry_delay * (2 ** attempt), 300)
            print(
                f"Upload attempt {attempt + 1} failed ({type(error).__name__}); "
                f"retrying in {delay}s ({attempt + 2}/{retries + 1})...",
                flush=True,
            )
            time.sleep(delay)


def login_to_mega(config, timeout):
    retries = int(config.get("mega_login_retries", config.get("upload_retries", 3)))
    retry_delay = int(config.get("upload_retry_delay_seconds", 30))
    if retries < 0 or retry_delay < 0:
        raise ValueError("MEGA login retries and retry delay must not be negative")

    for attempt in range(retries + 1):
        try:
            client = Mega()
            client.timeout = timeout
            return client.login(config["mega_email"], config["mega_password"])
        except RETRYABLE_MEGA_ERRORS as error:
            if attempt == retries:
                raise
            delay = min(retry_delay * (2 ** attempt), 300)
            print(
                f"MEGA login attempt {attempt + 1} failed ({type(error).__name__}); "
                f"retrying in {delay}s ({attempt + 2}/{retries + 1})...",
                flush=True,
            )
            time.sleep(delay)


def with_stage(stage, operation):
    try:
        return operation()
    except Exception as error:
        raise RuntimeError(f"{stage}: {error}") from error


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

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S_UTC")
    archive_prefix = config.get("archive_prefix", "MegaBackup").strip()
    if not archive_prefix or any(char in archive_prefix for char in ("/", "\\", "\0")):
        raise ValueError("archive_prefix must be a non-empty filename prefix")
    archive_name = f"{archive_prefix}-{os.uname().nodename}-{timestamp}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="MegaBackup-") as temp_dir:
        archive_path = Path(temp_dir) / archive_name

        print("Creating backup archive...", flush=True)
        with tarfile.open(archive_path, "w:gz") as archive:
            for path in paths:
                archive.add(path, arcname=path.as_posix().lstrip("/"))

        with enable_mega_hashcash():
            print("Connecting to MEGA...", flush=True)
            mega = with_stage(
                "Вход в MEGA",
                lambda: login_to_mega(config, mega_timeout),
            )
            folder_name = config.get("mega_folder", "MegaBackup")
            folder = with_stage(
                "Подготовка папки в MEGA",
                lambda: mega.create_folder(folder_name),
            )
            folder_id = folder[folder_name]
            print("Uploading archive to MEGA...", flush=True)
            with_stage(
                "Загрузка архива в MEGA",
                lambda: upload_with_retries(
                    mega, archive_path, folder_id, archive_name, config
                ),
            )

            print("Removing expired backups...", flush=True)
            archive_prefixes = config.get("archive_prefix_history", [])
            if not isinstance(archive_prefixes, list):
                archive_prefixes = []
            archive_prefixes = set(archive_prefixes) | {archive_prefix}
            removed_count = with_stage(
                "Очистка устаревших архивов",
                lambda: remove_expired_backups(
                    mega, folder_id, retention_days, archive_prefixes
                ),
            )
    return archive_name, removed_count


def remove_expired_backups(mega, folder_id, retention_days, archive_prefixes):
    cutoff = int(datetime.now(timezone.utc).timestamp()) - retention_days * 86400
    removed = 0

    for file_id, node in mega.get_files().items():
        attributes = node.get("a")
        if node.get("t") != 0 or node.get("p") != folder_id:
            continue
        if not isinstance(attributes, dict):
            continue
        filename = attributes.get("n", "")
        if not any(filename.startswith(f"{prefix}-") for prefix in archive_prefixes):
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
                send_telegram(
                    config,
                    "❌ <b>Резервное копирование не удалось</b>\n\n"
                    f"🖥 Сервер: <code>{html.escape(os.uname().nodename)}</code>\n"
                    f"⚠️ Причина: <code>{html.escape(str(error)[:600])}</code>",
                )
            except Exception as notify_error:
                print(f"Telegram notification failed: {notify_error}", file=sys.stderr)
            return 1

        print(f"Backup uploaded: {archive_name}")
        try:
            send_telegram(
                config,
                "✅ <b>Резервное копирование завершено</b>\n\n"
                f"🖥 Сервер: <code>{html.escape(os.uname().nodename)}</code>\n"
                f"📦 Архив: <code>{html.escape(archive_name)}</code>\n"
                "🛡 Защита: <b>отсутствует, обычный архив</b>\n"
                f"🗑 Удалено устаревших архивов: <b>{removed_count}</b>",
            )
        except (URLError, RuntimeError, KeyError) as error:
            print(f"Backup succeeded, but Telegram notification failed: {error}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
