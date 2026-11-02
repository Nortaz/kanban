# -*- coding: utf-8 -*-
"""Доска — Канбан и Дневник: Windows-оболочка (pywebview + WebView2).

Интерфейс целиком живёт в index.html. Python отвечает за надёжность:
атомарная запись board.json, ротация копий, один экземпляр, журнал ошибок.
"""
import ctypes
import json
import logging
import os
import shutil
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_NAME = "KanbanDiary"
TITLE = "Доска — Канбан и Дневник"
HTTP_PORT = 47831        # фиксированный порт: у страницы всегда один и тот же origin
LOCK_PORT = 47829        # занят = приложение уже запущено (освобождается само при падении)
SNAPSHOT_EVERY = 10 * 60  # секунд между копиями в backups/
SNAPSHOT_KEEP = 30
WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"

log = logging.getLogger("doska")


def base_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def data_dir() -> Path:
    root = os.environ.get("APPDATA") or str(Path.home() / ".config")
    folder = Path(root) / APP_NAME
    (folder / "backups").mkdir(parents=True, exist_ok=True)
    return folder


def setup_logging(folder: Path) -> None:
    handler = RotatingFileHandler(folder / "app.log", maxBytes=512 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])

    def hook(exc_type, exc, tb):
        log.critical("Необработанная ошибка", exc_info=(exc_type, exc, tb))
    sys.excepthook = hook


def message_box(text: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x40)
    else:
        print(text)


class Storage:
    """board.json + board.prev.json + backups/board-*.json. Запись атомарная."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.file = folder / "board.json"
        self.prev = folder / "board.prev.json"
        self.snaps = folder / "backups"
        self._lock = threading.Lock()
        self._last_text = None
        self._last_prev = 0.0
        self._last_snap = 0.0

    @staticmethod
    def valid(text) -> bool:
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return False
        return isinstance(data, dict) and isinstance(data.get("columns"), list)

    def load(self):
        candidates = [self.file, self.prev] + sorted(self.snaps.glob("board-*.json"), reverse=True)
        for path in candidates:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if self.valid(text):
                if path != self.file:
                    log.warning("board.json повреждён или отсутствует, данные взяты из %s", path.name)
                self._last_text = text
                return text
        return None

    def save(self, text) -> bool:
        if not self.valid(text):
            log.error("Отказ записи: данные не похожи на доску")
            return False
        with self._lock:
            if text == self._last_text:
                return True
            try:
                tmp = self.file.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(text)
                    f.flush()
                    os.fsync(f.fileno())
                now = time.time()
                if self.file.exists() and now - self._last_prev > 30:
                    shutil.copy2(self.file, self.prev)
                    self._last_prev = now
                os.replace(tmp, self.file)
                self._last_text = text
                self._snapshot(text)
                return True
            except OSError:
                log.exception("Не удалось записать board.json")
                return False

    def _snapshot(self, text) -> None:
        now = time.time()
        if now - self._last_snap < SNAPSHOT_EVERY:
            return
        self._last_snap = now
        name = "board-%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            (self.snaps / name).write_text(text, encoding="utf-8")
            for old in sorted(self.snaps.glob("board-*.json"))[:-SNAPSHOT_KEEP]:
                old.unlink(missing_ok=True)
        except OSError:
            log.exception("Не удалось сделать копию")

    def snapshot_now(self) -> None:
        if self._last_text:
            self._snapshot(self._last_text)


class Api:
    """Методы, доступные странице как window.pywebview.api.*"""

    def __init__(self, storage: Storage, folder: Path):
        self._storage = storage
        self._folder = folder

    def save_board(self, text):
        return self._storage.save(text)

    def export_json(self, text, name):
        """Нативный диалог «Сохранить как» (скачивание в WebView2 ненадёжно)."""
        import webview
        kind = webview.FileDialog.SAVE if hasattr(webview, "FileDialog") else webview.SAVE_DIALOG
        try:
            path = webview.windows[0].create_file_dialog(
                kind, save_filename=name, file_types=("JSON (*.json)", "Все файлы (*.*)"))
        except Exception:
            log.exception("Диалог сохранения не открылся")
            return ""
        if not path:
            return ""
        if isinstance(path, (list, tuple)):
            path = path[0]
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError:
            log.exception("Не удалось записать экспорт")
            return ""
        return str(path)

    def open_data_folder(self):
        if os.name == "nt":
            os.startfile(str(self._folder))  # noqa
        return str(self._folder)


def make_handler(storage: Storage, html_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                self.send_error(403)
                return
            if self.path.split("?")[0] not in ("/", "/index.html"):
                self.send_error(404)
                return
            try:
                page = html_path.read_text(encoding="utf-8")
                boot = storage.load()
                payload = boot.replace("</", "<\\/").replace("<!--", "<\\!--") if boot else "null"
                inject = (
                    "<script>window.__DESKTOP__=true;document.documentElement.classList.add('desktop');"
                    "window.__DESKTOP_BOARD__=%s;"
                    "try{var s=(window.__DESKTOP_BOARD__||{}).settings||{};"
                    "for(var k in s){if(localStorage.getItem(k)===null)localStorage.setItem(k,s[k]);}}catch(e){}"
                    "</script>\n" % payload
                )
                body = page.replace("<script>", inject + "<script>", 1).encode("utf-8")
            except Exception:
                log.exception("Не удалось отдать страницу")
                self.send_error(500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def start_server(storage: Storage, html_path: Path):
    handler = make_handler(storage, html_path)
    for port in range(HTTP_PORT, HTTP_PORT + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:
            continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, port
    raise RuntimeError("Нет свободного порта для встроенного сервера")


def acquire_lock():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
        sock.listen(1)
        return sock
    except OSError:
        return None


def webview2_installed() -> bool:
    if os.name != "nt":
        return True
    import winreg
    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    places = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients"),
    )
    for hive, sub in places:
        try:
            with winreg.OpenKey(hive, sub + "\\" + guid) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def load_settings(folder: Path) -> dict:
    try:
        data = json.loads((folder / "window.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def main() -> int:
    folder = data_dir()
    setup_logging(folder)
    log.info("Запуск, данные: %s", folder)

    lock = acquire_lock()
    if lock is None:
        message_box("Приложение уже запущено.")
        return 0
    if not webview2_installed():
        message_box("Для работы нужен Microsoft Edge WebView2 Runtime.\nСейчас откроется страница загрузки.")
        webbrowser.open(WEBVIEW2_URL)
        return 1
    html_path = base_dir() / "index.html"
    if not html_path.exists():
        message_box("Не найден файл index.html рядом с программой.")
        return 1

    import webview  # импорт здесь, чтобы ошибки установки попадали в журнал

    storage = Storage(folder)
    if storage.load():
        storage.snapshot_now()
    _server, port = start_server(storage, html_path)

    cfg = load_settings(folder)
    width = max(900, int(cfg.get("width", 1360)))
    height = max(600, int(cfg.get("height", 860)))
    opts = {}
    if isinstance(cfg.get("x"), int) and isinstance(cfg.get("y"), int) and -200 < cfg["x"] < 6000 and -50 < cfg["y"] < 4000:
        opts = {"x": cfg["x"], "y": cfg["y"]}

    window = webview.create_window(
        TITLE, url="http://127.0.0.1:%d/" % port, js_api=Api(storage, folder),
        width=width, height=height, min_size=(900, 600), text_select=True, **opts)

    def on_closing():
        try:
            state = {"width": window.width, "height": window.height, "x": window.x, "y": window.y}
            (folder / "window.json").write_text(json.dumps(state), encoding="utf-8")
        except Exception:
            log.exception("Не удалось сохранить размер окна")

    window.events.closing += on_closing
    webview.start(gui="edgechromium" if os.name == "nt" else None,
                  storage_path=str(folder / "webview"), private_mode=False, debug=False)
    log.info("Выход")
    return 0


if __name__ == "__main__":
    sys.exit(main())
