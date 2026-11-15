import sys
import os

if __name__ == "__main__":
    # Run from this folder no matter where it was launched from, so core.py
    # picks up THIS app's aiconfig/venom_ai.json and not FBPoison's.
    HERE = os.path.dirname(os.path.abspath(__file__))
    if HERE not in sys.path:
        sys.path.insert(0, HERE)

    if "--build-check" in sys.argv:
        # Windowed PyInstaller executables have no console, so packaging tests
        # write a small machine-readable report instead of relying on stdout.
        # This path deliberately stops before the license/auth UI.
        import json
        import aiconfig
        import core
        from PIL import Image
        from PyQt6.QtWidgets import QApplication
        from gui import app_icon_pixmap

        app = QApplication.instance() or QApplication(sys.argv)
        pm = app_icon_pixmap(256)
        report = {
            "frozen": bool(getattr(sys, "frozen", False)),
            "executable": sys.executable,
            "app_dir": core.HERE,
            "config_path": aiconfig.CONFIG_PATH,
            "config_beside_exe": (os.path.dirname(aiconfig.CONFIG_PATH) ==
                                   os.path.dirname(sys.executable)),
            "adb": core.ADB,
            "adb_exists": os.path.isfile(core.ADB),
            "icon_loaded": not pm.isNull(),
            "icon_size": [pm.width(), pm.height()],
            "pillow": Image.__version__ if hasattr(Image, "__version__") else "ok",
        }
        out = os.environ.get(
            "FBVENOM_BUILD_CHECK_PATH",
            os.path.join(os.path.dirname(sys.executable),
                         "FBVenom.build-check.json"))
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        sys.exit(0 if all((report["adb_exists"], report["icon_loaded"],
                           report["config_beside_exe"])) else 2)
    elif "--cli" in sys.argv:
        from core import cli_main
        cli_main()
    elif "--ai" in sys.argv:
        # Headless check of the AI endpoint: prove comments will generate
        # before committing a whole run to it.
        import aiconfig
        import core
        print("endpoint :", aiconfig.describe())
        print("ready    :", aiconfig.is_ready())
        example = "grabe ang ganda nito"
        print(f"example  : {example}")
        try:
            for i in range(3):
                print(f"  variant {i + 1}: {core.unique_comment(example)}")
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {e}")
            sys.exit(1)
    else:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtGui import QIcon
        from gui import ReactionGUI, app_icon_pixmap
        from auth import require_key
        app = QApplication(sys.argv)
        app.setStyle("Fusion")
        app.setWindowIcon(QIcon(app_icon_pixmap(256)))

        # Same locked gate as FBPoison: a wrong key plays the denial sequence
        # and never returns True, so the GUI is never built.
        if not require_key():
            sys.exit(0)

        gui = ReactionGUI()
        gui.show_splash()
        sys.exit(app.exec())
