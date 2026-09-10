#!/usr/bin/env python3
"""
Собрать портативный архив для тех, у кого ничего не установлено.

Внутри архива — свой Python и все библиотеки: ни интерпретатора, ни uv, ни
pip, ни доступа к PyPI на машине получателя не нужно. Распаковал, запустил
.bat — сервер прописан в агенте.

Собирать нужно тем же питоном, что лежит внутри архива (PYTHON_VERSION):
зависимости ставятся интерпретатором сборки, а работать будут в рантайме.

Запуск:
    uv run --python 3.11 python scripts/build_portable.py
"""

import argparse
import compileall
import hashlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = 'bitrix-mcp'

# Windows x64: внутри лежит embeddable-питон именно под эту платформу,
# и по имени файла в списке релиза это должно быть видно
PLATFORM = 'win64'


def version() -> str:
    """
    Версия пакета — из pyproject, а не второй копией здесь.

    Returns:
        Строка версии
    """
    import tomllib

    data = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    return data['project']['version']

PYTHON_VERSION = '3.11.9'
RUNTIME_URL = (f'https://www.python.org/ftp/python/{PYTHON_VERSION}/'
               f'python-{PYTHON_VERSION}-embed-amd64.zip')

# Без pyproject.toml намеренно: src/ в портативной сборке нет (пакет уже стоит
# в runtime), и uv, увидев здесь проект, соберёт пустое колесо и «успешно»
# установит его - с ModuleNotFoundError на запуске.
FILES = ('README.md', 'README.ru.md', 'ARCHITECTURE.md', 'ARCHITECTURE.ru.md',
         'ROADMAP.md', 'ROADMAP.ru.md', 'REQUIREMENTS.md', 'REQUIREMENTS.ru.md',
         'SECURITY.md', 'LICENSE', '.env.example')

LAUNCHER = r'''@echo off
chcp 866 >nul
title Bitrix24 MCP - podklyuchenie k agentu
setlocal
set "HERE=%~dp0"

echo.
echo   1. Proveryayu rantaym i biblioteki
"%HERE%runtime\python.exe" -c "import bitrix_mcp, mcp, httpx, pydantic, sys; print('      Python', sys.version.split()[0], '- vsyo na meste')"
if errorlevel 1 goto :fail

echo.
echo   2. Propisyvayu server v konfiguraciyu agenta
"%HERE%runtime\python.exe" "%HERE%install_mcp.py"
if errorlevel 1 goto :fail

echo.
echo   Gotovo. Perezapustite agenta.
echo.
pause
exit /b 0

:fail
echo.
echo   Ne poluchilos. Smotrite soobschenie vyshe.
echo.
pause
exit /b 1
'''

INSTALLER = '''#!/usr/bin/env python3
"""
Прописать сервер bitrix24 в конфигурацию агента.

Существующие серверы не затираются: читаем файл, добавляем свою запись,
пишем обратно.

    python install_mcp.py                   # спросит, какой агент
    python install_mcp.py --agent cursor    # без вопросов
"""

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = 'bitrix24'


def desktop_config() -> Path:
    """Файл настроек Claude Desktop — свой на каждой платформе."""
    if sys.platform == 'win32':
        base = Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming')
        return base / 'Claude' / 'claude_desktop_config.json'
    if sys.platform == 'darwin':
        return (Path.home() / 'Library' / 'Application Support' / 'Claude'
                / 'claude_desktop_config.json')
    return Path.home() / '.config' / 'Claude' / 'claude_desktop_config.json'


# Формат записи у всех троих одинаковый (mcpServers -> command/args),
# различаются только файлы.
AGENTS = {
    'claude': ('Claude Code', lambda: Path.home() / '.claude.json'),
    'claude-desktop': ('Claude Desktop', desktop_config),
    'cursor': ('Cursor', lambda: Path.home() / '.cursor' / 'mcp.json'),
}


def ask() -> str:
    """Спросить агента. Без консоли (запуск из скрипта) — Claude Code."""
    keys = list(AGENTS)
    if not sys.stdin or not sys.stdin.isatty():
        return keys[0]

    print('Куда прописать сервер?')
    for number, key in enumerate(keys, 1):
        print(f'  {number}. {AGENTS[key][0]}')
    while True:
        answer = input('Номер [1]: ').strip() or '1'
        if answer.isdigit() and 1 <= int(answer) <= len(keys):
            return keys[int(answer) - 1]
        print('Не понял. Введите номер из списка.')


def check_webhook() -> None:
    """
    Сказать про вебхук сейчас, а не после первого вызова.

    Сервер без вебхука стартует, отдаёт инструменты и выглядит здоровым —
    ошибку видно только в ответе на вызов, и выглядит она как поломка сервера.
    """
    env = HERE / '.env'
    if not env.is_file():
        print(f'ВНИМАНИЕ: нет файла {env}.')
        print('           Скопируйте .env.example в .env и впишите вебхук портала.')
        return

    for line in env.read_text(encoding='utf-8-sig', errors='replace').splitlines():
        key, _, value = line.strip().partition('=')
        if key.strip() != 'BITRIX_WEBHOOK_URL':
            continue
        value = value.strip().strip('"').strip("'")
        if value and 'your-portal' not in value:
            return
        break

    print(f'ВНИМАНИЕ: в {env} не заполнен BITRIX_WEBHOOK_URL —')
    print('           вызовы вернут NO_WEBHOOK.')


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Прописать сервер bitrix24 в конфигурацию агента')
    parser.add_argument('--agent', choices=list(AGENTS),
                        help='Агент. Без него будет вопрос.')
    args = parser.parse_args()

    label, locate = AGENTS[args.agent or ask()]
    path = locate()

    entry = {
        'command': str(HERE / 'runtime' / 'python.exe'),
        'args': ['-m', 'bitrix_mcp'],
        'cwd': str(HERE),
    }

    config = {}
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding='utf-8')) or {}
        except json.JSONDecodeError as e:
            print(f'ОШИБКА: {path} повреждён ({e}). Файл не тронут.')
            return 1

    servers = config.setdefault('mcpServers', {})
    existed = NAME in servers
    servers[NAME] = entry

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2),
                    encoding='utf-8')
    print(f"Сервер {NAME} {'обновлён' if existed else 'добавлен'}: {label}, {path}")

    check_webhook()
    return 0


if __name__ == '__main__':
    sys.exit(main())
'''

READ_ME = '''Bitrix24 MCP — шлюз к REST API Битрикс24 для агента
===================================================

Устанавливать ничего не нужно: Python и все библиотеки лежат внутри архива,
в каталоге runtime. Ни uv, ни pip, ни интернет для установки не требуются.

Порядок:

1. Распакуйте архив куда угодно, например в C:\\bitrix-mcp
   (в пути лучше без пробелов и кириллицы).

2. Скопируйте .env.example в .env и впишите вебхук своего портала:
     BITRIX_WEBHOOK_URL=https://<портал>/rest/<id>/<токен>/

   Вебхук — это учётные данные. Выдавайте его пользователю с минимально
   нужными правами, а не администратору. Подробности — в SECURITY.md.

3. Запустите «ПОДКЛЮЧИТЬ.bat» — он спросит, какой у вас агент
   (Claude Code, Claude Desktop или Cursor) — и перезапустите агента.

   То же самое без .bat:  runtime\\python.exe install_mcp.py --agent cursor

4. Скажите агенту, что нужно сделать в Битриксе. Если нужно только читать —
   поставьте в .env BITRIX_READ_ONLY=1, тогда запись заблокирована.

Чего не делать
--------------

Не запускайте эту папку через «uv run --directory .» — это строка для
установки из исходников, здесь она соберёт пустой пакет. Портативная сборка
работает своим рантаймом: runtime\\python.exe -m bitrix_mcp

Документация: README.ru.md, REQUIREMENTS.ru.md, SECURITY.md
'''


def log(message: str) -> None:
    print(message, flush=True)


def fetch_runtime() -> Path:
    """
    Взять embeddable-питон: он не требует установки и прав.

    Returns:
        Путь к zip с рантаймом
    """
    cache = ROOT.parent / f'python-{PYTHON_VERSION}-embed-amd64.zip'
    if cache.exists():
        log(f'рантайм уже скачан: {cache.name}')
        return cache

    log(f'качаю {RUNTIME_URL}')
    request = urllib.request.Request(
        RUNTIME_URL, headers={'User-Agent': 'bitrix-mcp/build'})
    with urllib.request.urlopen(request, timeout=300) as response:
        cache.write_bytes(response.read())

    return cache


def prepare_runtime(runtime_zip: Path, target: Path) -> None:
    """
    Развернуть рантайм и установить в него сам пакет со всеми зависимостями.

    Args:
        runtime_zip: Архив embeddable-питона
        target: Каталог runtime внутри сборки
    """
    with zipfile.ZipFile(runtime_zip) as z:
        z.extractall(target)

    # ._pth задаёт sys.path целиком и отключает рабочий каталог: объявляем
    # site-packages явно, иначе установленные библиотеки не найдутся
    for pth in target.glob('python*._pth'):
        lines = pth.read_text(encoding='utf-8').splitlines()
        lines = [line.replace('#import site', 'import site') for line in lines]
        if 'Lib\\site-packages' not in lines:
            lines.insert(lines.index('.') + 1 if '.' in lines else 0,
                         'Lib\\site-packages')
        pth.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    packages = target / 'Lib' / 'site-packages'
    packages.mkdir(parents=True, exist_ok=True)

    log('ставлю пакет и зависимости в рантайм')
    subprocess.run(
        [sys.executable, '-m', 'pip', 'install', '--quiet', '--target',
         str(packages), str(ROOT)],
        check=True, stdin=subprocess.DEVNULL
    )

    compileall.compile_dir(str(packages), quiet=2, force=False)


def check_builder() -> None:
    """
    Не дать собрать архив, который сломается только у получателя.

    Зависимости ставит интерпретатор сборки, а лежать они будут в рантайме
    PYTHON_VERSION. pydantic_core и rpds — бинарные и привязаны к версии
    (_pydantic_core.cp311-win_amd64.pyd), так что собранный, скажем, под 3.13
    архив выглядит целым, распаковывается, и падает ImportError на первом
    запуске у того, кому его отдали.
    """
    want = tuple(int(part) for part in PYTHON_VERSION.split('.')[:2])
    if sys.version_info[:2] != want:
        have = '.'.join(str(part) for part in sys.version_info[:2])
        raise SystemExit(
            f'Собирать нужно питоном {want[0]}.{want[1]} — рантайм внутри архива '
            f'именно такой, а бинарные колёса (pydantic_core, rpds) привязаны '
            f'к версии. Сейчас запущен {have}.\n'
            f'Например:  uv run --python {want[0]}.{want[1]} '
            f'python scripts/build_portable.py')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    check_builder()

    build = ROOT.parent / f'{NAME}-build'
    if build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)

    for name in FILES:
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, build / name)
        else:
            log(f'нет файла {name}, пропускаю')

    prepare_runtime(fetch_runtime(), build / 'runtime')

    (build / 'ПОДКЛЮЧИТЬ.bat').write_bytes(
        LAUNCHER.replace('\n', '\r\n').encode('cp866'))
    (build / 'install_mcp.py').write_text(INSTALLER, encoding='utf-8')
    (build / 'ЧИТАЙ МЕНЯ.txt').write_bytes(
        READ_ME.replace('\n', '\r\n').encode('utf-8-sig'))

    archive = ROOT / 'dist' / f'{NAME}-{version()}-portable-{PLATFORM}.zip'
    archive.parent.mkdir(exist_ok=True)
    if archive.exists():
        archive.unlink()

    log('пакую')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(build.rglob('*')):
            if path.is_file():
                z.write(path, Path(NAME) / path.relative_to(build))

    log('')
    log(f'готово: {archive}')
    log(f'размер: {archive.stat().st_size / 1048576:.1f} МБ')
    log(f'sha256: {hashlib.sha256(archive.read_bytes()).hexdigest()}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
