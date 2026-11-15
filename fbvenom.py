import sys
import os

def run_build_check():
    """Smoke-test a frozen Windows bundle without opening auth or using devices.

    ``adb version`` only inspects the bundled executable; it does not start an
    ADB server, enumerate phones, call an AI provider, or save app settings.
    Failures are written to JSON because a windowed EXE has no console.
    """
    import json
    import subprocess

    report = {
        "schema_version": 2,
        "frozen": bool(getattr(sys, "frozen", False)),
        "platform": sys.platform,
        "windows": sys.platform == "win32",
        "executable": sys.executable,
        "app_version": "",
        "app_dir": "",
        "config_path": "",
        "config_beside_exe": False,
        "app_beside_exe": False,
        "adb": "",
        "adb_exists": False,
        "adb_bundled": False,
        "adb_version_ok": False,
        "adb_version": "",
        "icon_loaded": False,
        "icon_size": [0, 0],
        "pillow": "",
        "auth_imported": False,
        "gui_features": {},
        "imports": {},
        "errors": [],
    }

    def failed(check, exc):
        report["errors"].append(f"{check}: {type(exc).__name__}: {exc}")

    def same_path(left, right):
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))

    try:
        from version import APP_VERSION
        report["app_version"] = str(APP_VERSION)
        report["imports"]["version"] = True
    except Exception as exc:
        report["imports"]["version"] = False
        failed("version import", exc)
    try:
        import aiconfig
        report["imports"]["aiconfig"] = True
        report["config_path"] = aiconfig.CONFIG_PATH
        report["config_beside_exe"] = same_path(
            os.path.dirname(aiconfig.CONFIG_PATH), os.path.dirname(sys.executable))
    except Exception as exc:
        report["imports"]["aiconfig"] = False
        failed("config import", exc)
    try:
        import core
        report["imports"]["core"] = True
        report["app_dir"] = core.HERE
        report["app_beside_exe"] = same_path(core.HERE, os.path.dirname(sys.executable))
        report["adb"] = core.ADB
        report["adb_exists"] = os.path.isfile(core.ADB)
        bundle = getattr(sys, "_MEIPASS", "")
        bundled_adb = os.path.join(bundle, "platform-tools", "adb.exe") if bundle else ""
        report["adb_bundled"] = bool(bundled_adb and report["adb_exists"]
                                      and same_path(core.ADB, bundled_adb))
        report["gui_features"]["live_link_normalization"] = (
            core.normalize_url("https://m.facebook.com/live/1234567890/?fbclid=build-check")
            == "https://www.facebook.com/live/1234567890")
        report["gui_features"]["structured_errors"] = callable(getattr(core, "report_error", None))
        # Never fall back to a host-installed ADB during a packaging check.
        if report["frozen"] and report["windows"] and report["adb_bundled"]:
            try:
                adb_result = subprocess.run(
                    [bundled_adb, "version"], stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=10, check=False)
                output = (adb_result.stdout or "") + (adb_result.stderr or "")
                report["adb_returncode"] = adb_result.returncode
                report["adb_version"] = output.strip()[:1500]
                report["adb_version_ok"] = (adb_result.returncode == 0
                    and "Android Debug Bridge version" in output)
            except Exception as exc:
                failed("bundled adb version", exc)
    except Exception as exc:
        report["imports"]["core"] = False
        failed("core import/check", exc)
    try:
        from PIL import Image
        report["imports"]["Pillow"] = True
        report["pillow"] = getattr(Image, "__version__", "ok")
    except Exception as exc:
        report["imports"]["Pillow"] = False
        failed("Pillow import", exc)
    try:
        from PyQt6.QtWidgets import QApplication
        import gui
        app = QApplication.instance() or QApplication([sys.argv[0]])
        pm = gui.app_icon_pixmap(256)
        report["imports"]["gui"] = True
        report["icon_loaded"] = not pm.isNull()
        report["icon_size"] = [pm.width(), pm.height()]
        report["gui_features"]["venom_theme"] = (callable(getattr(gui, "draw_symbiote", None))
                                                    and hasattr(gui, "SymbioteBanner"))
        report["gui_features"]["diagnostics_panel"] = hasattr(gui.ReactionGUI, "_build_diagnostics")
    except Exception as exc:
        report["imports"]["gui"] = False
        failed("GUI import/check", exc)
    try:
        import auth
        # Importing the gate checks its packaged dependencies. Do not call it.
        report["auth_imported"] = callable(getattr(auth, "require_key", None))
        report["imports"]["auth"] = report["auth_imported"]
    except Exception as exc:
        report["imports"]["auth"] = False
        failed("auth import", exc)

    checks = {name: report[name] for name in (
        "frozen", "windows", "config_beside_exe", "app_beside_exe",
        "adb_exists", "adb_bundled", "adb_version_ok", "icon_loaded", "auth_imported")}
    checks["app_version"] = bool(report["app_version"])
    checks["imports"] = all(report["imports"].values())
    checks["updated_features"] = all(report["gui_features"].get(name, False) for name in (
        "live_link_normalization", "structured_errors", "venom_theme", "diagnostics_panel"))
    report["failed_checks"] = [name for name, passed in checks.items() if not passed]
    report["ok"] = not report["failed_checks"] and not report["errors"]
    out = os.environ.get(
        "FBVENOM_BUILD_CHECK_PATH",
        os.path.join(os.path.dirname(sys.executable), "FBVenom.build-check.json"))
    try:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
            fh.write("\n")
    except OSError as exc:
        if sys.stderr is not None:
            print(f"Could not write build-check report: {exc}", file=sys.stderr)
        return 2
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    # Run from this folder no matter where it was launched from, so core.py
    # picks up THIS app's aiconfig/venom_ai.json and not FBPoison's.
    HERE = os.path.dirname(os.path.abspath(__file__))
    if HERE not in sys.path:
        sys.path.insert(0, HERE)

    if "--build-check" in sys.argv:
        sys.exit(run_build_check())
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
