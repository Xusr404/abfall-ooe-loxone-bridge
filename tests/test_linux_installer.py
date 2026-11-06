"""Exercise the real shell scripts in temporary directories with fake system tools.

No service, account, package, or system path is changed; no network is used.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, skipUnless


@skipUnless(sys.platform == "linux" and shutil.which("bash"), "Linux shell integration tests")
class LinuxInstallerTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bridge-installer-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        project = Path(__file__).resolve().parents[1]
        for folder in ("bridge", "tools/helpers", "docs"):
            shutil.copytree(project / folder, self.source / folder,
                            ignore=shutil.ignore_patterns("data", "__pycache__", ".venv"))
        for name in ("README.md", "LICENSE"):
            shutil.copyfile(project / name, self.source / name)
        self.application = self.root / "application"
        self.config = self.root / "settings"
        self.state = self.root / "state"
        self.logs = self.root / "logs"
        self.unit = self.root / "service.unit"
        self.calls = self.root / "calls"
        (self.root / "systemd").mkdir()
        (self.root / "os-release").write_text("ID=debian\n")
        replacements = {
            "/opt/loxone-bridge": str(self.application), "/etc/loxone-bridge": str(self.config),
            "/var/lib/loxone-bridge": str(self.state), "/var/log/loxone-bridge": str(self.logs),
            "/etc/systemd/system/loxone-bridge.service": str(self.unit),
            "/etc/os-release": str(self.root / "os-release"),
            "/run/systemd/system": str(self.root / "systemd"), '"${EUID}"': '"0"',
        }
        for name in ("install_loxone_bridge.sh", "uninstall_loxone_bridge.sh"):
            path = self.source / "bridge" / name
            text = path.read_text()
            for old, new in replacements.items():
                text = text.replace(old, new)
            path.write_text(text)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "TEST_ROOT": str(self.root)}
        # The driver replaces root-only operations and pip/service calls. Real tar,
        # file installation, release switches, migrations, and rollback still run.
        driver = '''import os, pathlib, subprocess, sys
root = pathlib.Path(os.environ["TEST_ROOT"])
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / "calls").open("a") as f: f.write(name + " " + " ".join(args) + "\\n")
if name == "python3":
    if args[:2] == ["-m", "venv"]:
        target = pathlib.Path(args[2]) / "bin"
        target.mkdir(parents=True)
        shim = target / "python"
        shim.write_text("#!" + sys.executable + "\\n" + pathlib.Path(__file__).read_text())
        shim.chmod(0o755)
    sys.exit(0)
if name == "python":
    if args[:3] == ["-m", "pip", "install"] and os.environ.get("FAIL_DEPENDENCIES"): sys.exit(1)
    if args and args[0].endswith("check_service_health.py") and os.environ.get("FAIL_STARTUP"): sys.exit(1)
    sys.exit(0)
if name == "getent":
    if args[0] == "passwd": print("loxone-bridge:x:1001:1001::/nonexistent:/usr/sbin/nologin")
    else: print("loxone-bridge:x:1001:")
elif name == "id": print("1001")
elif name == "systemctl":
    command = args[0]
    if command == "is-active": sys.exit(0 if (root / "active").exists() else 1)
    if command == "is-enabled": sys.exit(0 if (root / "enabled").exists() else 1)
    if command in ("start", "restart"): (root / "active").touch()
    if command == "enable": (root / "enabled").touch()
    if command == "stop": (root / "active").unlink(missing_ok=True)
    if command == "disable":
        (root / "enabled").unlink(missing_ok=True)
        if "--now" in args: (root / "active").unlink(missing_ok=True)
elif name == "install":
    cleaned = []
    i = 0
    while i < len(args):
        if args[i] in ("-o", "-g"): i += 2
        else: cleaned.append(args[i]); i += 1
    sys.exit(subprocess.call(["/usr/bin/install", *cleaned]))
'''
        for name in ("python3", "systemctl", "journalctl", "getent", "id", "chown", "install", "useradd", "groupadd", "userdel", "groupdel"):
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n" + driver)
            path.chmod(0o755)

    def run_script(self, name="install_loxone_bridge.sh", *args, success=True, **environment):
        result = subprocess.run(["bash", str(self.source / "bridge" / name), *args], env={**self.env, **environment},
                                capture_output=True, text=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_install_then_update_preserves_configuration_and_saved_data(self):
        self.run_script()
        first = (self.application / "current").resolve()
        settings = self.config / "bridge.env"
        settings.write_text("LOXONE_BRIDGE_PORT=9000\nLOXONE_BRIDGE_API_KEY=synthetic-key\n")
        (self.state / "address-data.json").write_text('{"synthetic":true}')
        self.run_script()
        self.assertNotEqual((self.application / "current").resolve(), first)
        self.assertIn("9000", settings.read_text())
        self.assertEqual((self.state / "address-data.json").read_text(), '{"synthetic":true}')
        self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
        unit = self.unit.read_text()
        self.assertIn("User=loxone-bridge", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertNotIn("refresh_address_data.py", self.calls.read_text())

    def test_migration_copies_old_data_without_downloading(self):
        for folder, name in (("bridge/data", "loxone_bridge_cache.json"), ("tools/helpers/data", "address-data.json")):
            old = self.source / folder / name
            old.parent.mkdir(parents=True)
            old.write_text('{"synthetic":true}')
        self.run_script()
        for name in ("loxone_bridge_cache.json", "address-data.json"):
            self.assertEqual((self.state / name).read_text(), '{"synthetic":true}')
        self.assertFalse((self.application / "current" / "bridge" / "data").exists())

    def test_dependency_failure_leaves_running_release_untouched(self):
        self.run_script()
        previous = (self.application / "current").resolve()
        self.calls.write_text("")
        self.run_script(success=False, FAIL_DEPENDENCIES="1")
        self.assertEqual((self.application / "current").resolve(), previous)
        self.assertTrue((self.root / "active").exists())
        self.assertNotIn("systemctl stop", self.calls.read_text())

    def test_failed_startup_restores_previous_release_and_service(self):
        self.run_script()
        previous = (self.application / "current").resolve()
        old_unit = self.unit.read_text()
        self.run_script(success=False, FAIL_STARTUP="1")
        self.assertEqual((self.application / "current").resolve(), previous)
        self.assertEqual(self.unit.read_text(), old_unit)
        self.assertTrue((self.root / "active").exists())

    def test_uninstall_keeps_settings_and_data_by_default(self):
        self.run_script()
        self.run_script("uninstall_loxone_bridge.sh")
        self.assertFalse(self.application.exists())
        self.assertFalse(self.unit.exists())
        self.assertTrue((self.config / "bridge.env").exists())
        self.assertTrue(self.state.exists())

    def test_explicit_purge_removes_saved_settings_and_data(self):
        self.run_script()
        self.run_script("uninstall_loxone_bridge.sh", "--purge")
        for path in (self.application, self.config, self.state, self.logs, self.unit):
            self.assertFalse(path.exists())

    def test_generated_service_passes_systemd_validation(self):
        if not shutil.which("systemd-analyze"):
            self.skipTest("systemd-analyze is unavailable")
        self.run_script()
        definition = self.root / "loxone-bridge.service"
        shutil.copyfile(self.unit, definition)
        result = subprocess.run(["systemd-analyze", "verify", str(definition)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
