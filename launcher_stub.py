import ctypes
import os
import subprocess
import sys


def main() -> int:
    install_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "My Bookshelf")
    pythonw = os.path.join(install_dir, ".venv", "Scripts", "pythonw.exe")
    script = os.path.join(install_dir, "core", "desktop.py")

    if not os.path.exists(pythonw):
        ctypes.windll.user32.MessageBoxW(
            0,
            "My Bookshelf가 설치되어 있지 않습니다.\nSetup.exe를 먼저 실행해 주세요.",
            "My Bookshelf",
            0x10,
        )
        return 1

    subprocess.Popen(
        [pythonw, script],
        cwd=install_dir,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
