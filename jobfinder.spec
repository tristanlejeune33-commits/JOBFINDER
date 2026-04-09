# -*- mode: python ; coding: utf-8 -*-
"""
JobFinder — PyInstaller spec
Build : build_exe.bat
"""

import os

block_cipher = None

# ── Fichiers de données à embarquer dans le .exe ──────────────────────────
datas = [
    # (source, destination_dans_le_bundle)
    ('ui.html',                         '.'),
    ('cv_vibe_modern_html (3).html',    '.'),
]

# ── Modules cachés (Flask, Jinja2, SQLite…) ───────────────────────────────
hiddenimports = [
    'flask',
    'flask.templating',
    'jinja2',
    'jinja2.ext',
    'werkzeug',
    'werkzeug.serving',
    'werkzeug.routing',
    'werkzeug.middleware.proxy_fix',
    'anthropic',
    'openai',
    'requests',
    'bs4',
    'pypdf',
    'cloudscraper',
    'sqlite3',
    'json',
    'threading',
    'webbrowser',
]

# ── Modules exclus (trop lourds / inutiles dans l'exe) ────────────────────
excludes = [
    'playwright',       # trop lourd — PDF via html2pdf.js côté client
    'pytest',
    'matplotlib',
    'numpy',
    'pandas',
    'PIL',
    'tkinter',
    'PyQt5',
    'PyQt6',
    'wx',
    'gi',
    'test',
    'unittest',
]

a = Analysis(
    ['launcher.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='JobFinder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # True = fenêtre console visible (logs utiles)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='icon.ico',     # décommente si tu ajoutes une icône
)
