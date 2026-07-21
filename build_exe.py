"""Package WebDownloader as a single standalone executable using PyInstaller.

Run on the platform you want the binary for:

    python build_exe.py

On Windows this produces `dist/WebDownloader.exe` - a single file you can
copy to any Windows PC and run without installing Python or anything else.
(On macOS/Linux it produces a native single-file binary for that OS.)
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SEP = os.pathsep  # ';' on Windows, ':' elsewhere - PyInstaller --add-data separator


def _ensure(pkg: str, import_name: str | None = None) -> None:
    try:
        __import__(import_name or pkg)
    except ImportError:
        print(f"Installing {pkg} ...")
        subprocess.run([sys.executable, "-m", "pip", "install", pkg], check=True)


def main() -> None:
    # Runtime deps must be importable so PyInstaller can trace them.
    _ensure("Flask", "flask")
    _ensure("requests")
    _ensure("beautifulsoup4", "bs4")
    _ensure("pyinstaller", "PyInstaller")

    args = [
        sys.executable, "-m", "PyInstaller",
        "--name", "WebDownloader",
        "--onefile",
        "--clean",
        "--noconfirm",
        # Bundle the web UI so Flask can find it inside the frozen app.
        "--add-data", f"templates{SEP}templates",
        "--add-data", f"static{SEP}static",
        # The native folder picker uses tkinter; make sure it's included.
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.filedialog",
        "app.py",
    ]
    print("Running:", " ".join(args))
    subprocess.run(args, check=True, cwd=HERE)

    exe = os.path.join(HERE, "dist", "WebDownloader" + (".exe" if os.name == "nt" else ""))
    print("\nBuild complete.")
    print(f"Standalone executable: {exe}")


if __name__ == "__main__":
    main()
