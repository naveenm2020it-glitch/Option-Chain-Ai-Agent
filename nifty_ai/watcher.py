import threading
import time
from pathlib import Path
from .config import INCOMING_DIR
from .ingest import import_path

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except Exception:  # watcher is optional; manual scan/upload still work
    Observer = None
    FileSystemEventHandler = object


class _Handler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle(event.dest_path)

    def _handle(self, raw_path):
        path = Path(raw_path)
        if path.suffix.lower() in {".csv", ".zip", ".xls", ".xlsx", ".dat", ".txt"}:
            try:
                import_path(path)
            except Exception:
                pass


class FolderWatcher:
    def __init__(self):
        self.observer = None
        self.thread = None
        self.enabled = False

    def start(self):
        if Observer is None:
            return False
        if self.observer:
            return True
        INCOMING_DIR.mkdir(parents=True, exist_ok=True)
        self.observer = Observer()
        self.observer.schedule(_Handler(), str(INCOMING_DIR), recursive=True)
        self.observer.start()
        self.enabled = True
        return True

    def stop(self):
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=3)
            self.observer = None
        self.enabled = False
