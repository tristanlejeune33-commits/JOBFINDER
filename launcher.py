"""
JobFinder — PyInstaller entry point.
Lance le serveur Flask et ouvre le navigateur automatiquement.
"""
import sys
import os
import threading
import webbrowser
import time

# ── Environnement PyInstaller ──────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    # Exécutable compilé : BASE_DIR = dossier du .exe
    BASE_DIR = os.path.dirname(sys.executable)
    # Ajoute le répertoire d'extraction temporaire de PyInstaller au path
    if hasattr(sys, '_MEIPASS'):
        sys.path.insert(0, sys._MEIPASS)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Se placer dans le bon répertoire pour que SQLite et les assets soient trouvés
os.chdir(BASE_DIR)
os.environ.setdefault('JOBFINDER_BASE', BASE_DIR)

# ── Import de l'application Flask ──────────────────────────────────────────
from jobfinder import app, init_db  # noqa: E402

PORT = 5151


def _open_browser():
    """Attend que Flask soit prêt avant d'ouvrir le navigateur."""
    time.sleep(1.8)
    webbrowser.open(f'http://localhost:{PORT}')


if __name__ == '__main__':
    init_db()
    threading.Thread(target=_open_browser, daemon=True).start()
    print(f"\n  ⚡  JobFinder  →  http://localhost:{PORT}\n")
    app.run(
        host='127.0.0.1',
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
