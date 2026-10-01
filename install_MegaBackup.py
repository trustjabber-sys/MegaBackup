#!/usr/bin/env python3
import fcntl
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


APP_DIR = Path("/opt/MegaBackup")
VENV_DIR = APP_DIR / "venv"
VENV_PYTHON = VENV_DIR / "bin/python"
CONFIG_PATH = Path("/etc/MegaBackup.json")
CRON_PATH = Path("/etc/cron.d/MegaBackup")
SCRIPT_PATH = Path("/usr/local/sbin/MegaBackup.py")
LOG_PATH = Path("/var/log/MegaBackup.log")
LOCK_PATH = Path(tempfile.gettempdir()) / f"MegaBackup-{os.getuid()}.lock"

USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
COLORS = {
    "cyan": "\033[96m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "red": "\033[91m",
    "dim": "\033[2m",
    "bold": "\033[1m",
}
RESET = "\033[0m"


def paint(text, color):
    if not USE_COLOR:
        return text
    return f"{COLORS[color]}{text}{RESET}"


def show_banner():
    print()
    print(paint("  +--------------------------------------------------+", "cyan"))
    print(paint("  |                 MEGA BACKUP                     |", "cyan"))
    print(paint("  |       Установка автоматического бэкапа           |", "cyan"))
    print(paint("  +--------------------------------------------------+", "cyan"))
    print(paint("  MEGA  /  Telegram  /  расписание  /  хранение", "dim"))
    print()


def show_step(number, title):
    print(f"\n{paint(f'[{number}/4]', 'cyan')} {paint(title, 'bold')}")


def show_ok(text):
    print(f"  {paint('OK', 'green')}  {text}")


def show_note(text):
    print(f"  {paint('--', 'yellow')}  {text}")


def choose_action():
    print("  [1] Установить MegaBackup")
    print("  [2] Удалить MegaBackup с сервера")
    print("  [3] Изменить расписание бэкапа")
    return input("\n  Выберите действие [1/2/3]: ").strip()


def uninstall():
    show_step("2", "Удаление MegaBackup")
    print("  Будут удалены локальные компоненты:")
    for path in (APP_DIR, SCRIPT_PATH, CONFIG_PATH, CRON_PATH, LOG_PATH):
        print(f"    - {path}")
    print(paint("  Копии в облаке MEGA удаляться не будут.", "yellow"))
    if input("\n  Для подтверждения введите DELETE: ").strip() != "DELETE":
        show_note("Удаление отменено.")
        return 0

    try:
        with open(LOCK_PATH, "a", encoding="utf-8") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(
                    "Сейчас выполняется бэкап. Дождитесь его завершения и повторите удаление."
                ) from error

            for path in (CRON_PATH, CONFIG_PATH, SCRIPT_PATH, LOG_PATH):
                path.unlink(missing_ok=True)

            legacy_paths = (
                Path("/etc/mega-backup.json"),
                Path("/etc/cron.d/mega-backup"),
                Path("/usr/local/sbin/mega_backup.py"),
                Path("/var/log/mega-backup.log"),
            )
            for path in legacy_paths:
                path.unlink(missing_ok=True)

            if APP_DIR.exists():
                shutil.rmtree(APP_DIR)

        print()
        print(paint("  MegaBackup удалён с сервера.", "green"))
        print("  Архивы и папка MegaBackup в облаке MEGA сохранены.\n")
    except OSError as error:
        raise RuntimeError(f"Не удалось удалить локальные файлы: {error}") from error
    return 0


def run(command, label):
    print(f"  {paint('...', 'cyan')} {label}", flush=True)
    environment = os.environ.copy()
    environment["DEBIAN_FRONTEND"] = "noninteractive"
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if result.returncode:
        details = (result.stderr or result.stdout).strip().splitlines()
        tail = "\n".join(details[-8:])
        raise RuntimeError(f"{label} failed:\n{tail}")
    show_ok(label)


def read_os_release():
    values = {}
    try:
        lines = Path("/etc/os-release").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RuntimeError("Cannot read /etc/os-release") from error
    for line in lines:
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value.strip().strip('"').strip("'")
    return values


def require_supported_system():
    os_info = read_os_release()
    distro = os_info.get("ID", "").lower()
    distro_like = os_info.get("ID_LIKE", "").lower().split()
    if distro not in {"ubuntu", "debian"} and "debian" not in distro_like:
        raise RuntimeError("This installer currently supports Ubuntu and Debian only")
    if os.geteuid() != 0:
        raise RuntimeError("Запустите установщик от root: python3 install_MegaBackup.py")


def ask_backup_paths():
    while True:
        raw = input(
            "  Папки для бэкапа через запятую "
            "(например: /home/user/Documents, /var/www): "
        ).strip()
        paths = [part.strip() for part in raw.split(",") if part.strip()]
        if not paths:
            show_note("Укажите хотя бы одну папку.")
            continue
        invalid = [
            path for path in paths
            if not Path(path).is_absolute() or not Path(path).is_dir()
        ]
        if invalid:
            show_note("Папки должны существовать и быть абсолютными: " + ", ".join(invalid))
            continue
        return list(dict.fromkeys(paths))


def ask_retention_days():
    while True:
        value = input("  Хранить копии, дней [30]: ").strip() or "30"
        try:
            days = int(value)
            if days > 0:
                return days
        except ValueError:
            pass
        show_note("Введите целое число больше нуля.")


def ask_schedule(default="02:30"):
    while True:
        value = input(
            f"  Время ежедневного запуска, местное [{default}]: "
        ).strip() or default
        match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value)
        if match:
            return match.group(1), match.group(2)
        show_note("Используйте формат ЧЧ:ММ, например 02:30.")


def ask_archive_prefix(default="MegaBackup"):
    while True:
        value = input(
            f"  Имя архива без даты и расширения [{default}]: "
        ).strip() or default
        if value not in {".", ".."} and not any(
            char in value for char in ("/", "\\", "\0")
        ) and not any(ord(char) < 32 or ord(char) == 127 for char in value):
            return value
        show_note("Имя не должно содержать /, \\, или управляющие символы.")


def save_config(config):
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    CONFIG_PATH.chmod(0o600)


def write_cron_schedule(hour, minute):
    cron_entry = (
        "SHELL=/bin/sh\n"
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
        f"{minute} {hour} * * * root {VENV_PYTHON} {SCRIPT_PATH} "
        f">> {LOG_PATH} 2>&1\n"
    )
    CRON_PATH.write_text(cron_entry, encoding="utf-8")
    CRON_PATH.chmod(0o644)


def deploy_backup_script():
    source_script = Path(__file__).resolve().with_name("MegaBackup.py")
    if not source_script.is_file():
        raise RuntimeError("Положите MegaBackup.py рядом с установщиком.")
    SCRIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_script, SCRIPT_PATH)
    SCRIPT_PATH.chmod(0o755)
    if CONFIG_PATH.is_file():
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if "archive_password" in config:
            config.pop("archive_password")
            save_config(config)


def update_schedule():
    if not CRON_PATH.is_file():
        raise RuntimeError("Установка не найдена: файл расписания отсутствует.")
    deploy_backup_script()

    default_time = "02:30"
    for line in CRON_PATH.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[5] == "root":
            default_time = f"{fields[1]}:{fields[0]}"
            break

    hour, minute = ask_schedule(default_time)
    write_cron_schedule(hour, minute)
    run(["systemctl", "enable", "--now", "cron"], "Применение расписания")
    show_ok(f"Бэкап будет запускаться ежедневно в {hour}:{minute}.")
    return 0


def collect_config():
    print(paint("  Укажите источники, доступы и параметры хранения.", "dim"))
    backup_paths = ask_backup_paths()
    mega_folder = input("  Корневая папка на MEGA [MegaBackup]: ").strip() or "MegaBackup"
    archive_prefix = ask_archive_prefix()
    mega_email = input("  Email MEGA: ").strip()
    mega_password = getpass.getpass("  Пароль MEGA: ")
    telegram_bot_token = getpass.getpass("  Токен Telegram-бота: ")
    telegram_chat_id = input("  Telegram Chat ID: ").strip()
    retention_days = ask_retention_days()
    hour, minute = ask_schedule()

    if not mega_email or not mega_password or not telegram_bot_token or not telegram_chat_id:
        raise RuntimeError("MEGA and Telegram settings must not be empty")

    config = {
        "backup_paths": backup_paths,
        "mega_email": mega_email,
        "mega_password": mega_password,
        "mega_folder": mega_folder,
        "archive_prefix": archive_prefix,
        "archive_prefix_history": [],
        "telegram_bot_token": telegram_bot_token,
        "telegram_chat_id": telegram_chat_id,
        "retention_days": retention_days,
    }
    return config, hour, minute


def main():
    try:
        show_banner()
        show_step("1", "Проверка системы")
        require_supported_system()
        action = choose_action()
        if action == "2":
            return uninstall()
        if action == "3":
            return update_schedule()
        if action != "1":
            show_note("Действие отменено.")
            return 0

        source_script = Path(__file__).resolve().with_name("MegaBackup.py")
        if not source_script.is_file():
            raise RuntimeError("Положите MegaBackup.py рядом с установщиком.")
        legacy_paths = (
            Path("/etc/mega-backup.json"),
            Path("/etc/cron.d/mega-backup"),
            Path("/usr/local/sbin/mega_backup.py"),
        )
        if CONFIG_PATH.exists() or CRON_PATH.exists() or any(path.exists() for path in legacy_paths):
            raise RuntimeError(
                "Найдена существующая установка. Она не будет перезаписана; "
                "сначала сохраните её настройки или удалите старую версию."
            )

        show_ok("Поддерживаемая система и файлы найдены.")
        show_step("2", "Настройка бэкапа")
        config, hour, minute = collect_config()

        show_step("3", "Установка компонентов")
        run(["apt-get", "update"], "Обновление списка пакетов")
        run(
            ["apt-get", "install", "-y", "python3", "python3-venv", "cron"],
            "Установка Python и cron",
        )

        APP_DIR.mkdir(parents=True, exist_ok=True)
        if not VENV_PYTHON.exists():
            run(["python3", "-m", "venv", str(VENV_DIR)], "Создание виртуального окружения")
        run(
            [str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip"],
            "Обновление pip",
        )
        run([str(VENV_PYTHON), "-m", "pip", "install", "mega.py"], "Установка MEGA-клиента")
        run(
            [
                str(VENV_PYTHON), "-m", "pip", "install", "--upgrade",
                "tenacity>=8,<10",
            ],
            "Настройка совместимости Python",
        )
        run([
            str(VENV_PYTHON), "-c",
            "from mega import Mega; print('MEGA client import OK')",
        ], "Проверка MEGA-клиента")

        show_step("4", "Установка MegaBackup")
        deploy_backup_script()

        save_config(config)
        write_cron_schedule(hour, minute)
        LOG_PATH.touch(exist_ok=True)
        run(["systemctl", "enable", "--now", "cron"], "Включение службы cron")

        print()
        print(paint("  +--------------------------------------------------+", "green"))
        print(paint("  |             УСТАНОВКА ЗАВЕРШЕНА                 |", "green"))
        print(paint("  +--------------------------------------------------+", "green"))
        print(f"  Скрипт:      {SCRIPT_PATH}")
        print(f"  Настройки:   {CONFIG_PATH}")
        print(f"  MEGA-папка:  {config['mega_folder']}")
        print("  Защита:      отсутствует (обычный архив .tar.gz)")
        print(f"  Хранение:    {config['retention_days']} дн.")
        print(f"  Расписание:  ежедневно в {hour}:{minute}")
        print(f"  Лог:         {LOG_PATH}")
        print("\n  Ручной запуск:")
        print(f"    {VENV_PYTHON} {SCRIPT_PATH}")
        print("\n  Спасибо, что выбрали MegaBackup.\n")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"\n{paint('УСТАНОВКА НЕ ЗАВЕРШЕНА', 'red')}", file=sys.stderr)
        print(f"{error}\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
