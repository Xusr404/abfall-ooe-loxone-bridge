"""Installer settings and health verification, without external requests."""

import io
import os
import subprocess
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from tests import support
from check_service_health import health_url, main as check_health
from loxone_bridge import parse_args


class InstallationSettingsTests(TestCase):
    def test_service_settings_come_from_environment(self):
        values = {"LOXONE_BRIDGE_HOST": "127.0.0.1", "LOXONE_BRIDGE_PORT": "9000", "LOXONE_BRIDGE_API_KEY": "test-key"}
        with patch.dict(os.environ, values, clear=True), patch.object(sys, "argv", ["bridge"]):
            args = parse_args()
        self.assertEqual((args.host, args.port, args.api_key), ("127.0.0.1", 9000, "test-key"))

    def test_command_line_settings_override_environment(self):
        with patch.dict(os.environ, {"LOXONE_BRIDGE_PORT": "9000"}), patch.object(sys, "argv", ["bridge", "--port", "9001"]):
            self.assertEqual(parse_args().port, 9001)

    def test_invalid_port_fails_before_starting(self):
        for value in ("0", "65536", "invalid"):
            with self.subTest(port=value), patch.object(sys, "argv", ["bridge", "--port", value]), patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit):
                    parse_args()

    def test_snapshot_override_is_shared_by_helper_and_refresh_utility(self):
        # A fresh process checks import-time settings without changing loaded modules.
        script = (
            "import sys; sys.path.insert(0, 'bridge'); import loxone_bridge; "
            "import address_helper_server as helper; import refresh_address_data as refresh; "
            "assert helper.ADDRESS_DATA_FILE == refresh.DEFAULT_OUTPUT; "
            "assert str(refresh.DEFAULT_OUTPUT) == 'custom-address-data.json'"
        )
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                env={**os.environ, "LOXONE_BRIDGE_ADDRESS_DATA_FILE": "custom-address-data.json"},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class LocalHealthTests(TestCase):
    def test_health_url_handles_ipv4_ipv6_and_custom_port(self):
        for host, expected in (("0.0.0.0", "127.0.0.1"), ("::", "[::1]"), ("localhost", "127.0.0.1"), ("127.0.0.1", "127.0.0.1")):
            with self.subTest(host=host):
                self.assertEqual(health_url({"LOXONE_BRIDGE_HOST": host, "LOXONE_BRIDGE_PORT": "9000"}), f"http://{expected}:9000/health")

    def test_health_url_rejects_hostnames_and_invalid_ports(self):
        for settings in ({"LOXONE_BRIDGE_HOST": "example.invalid"}, {"LOXONE_BRIDGE_PORT": "0"}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                health_url(settings)

    def test_health_checker_reads_effective_settings_and_accepts_local_success(self):
        response = io.BytesIO(b'{"status":"ok"}')
        response.status = 200
        with patch("check_service_health.subprocess.check_output", return_value="123\n"), \
             patch("check_service_health.Path.read_bytes", return_value=b"LOXONE_BRIDGE_HOST=0.0.0.0\0LOXONE_BRIDGE_PORT=9000\0"), \
             patch("check_service_health.build_opener") as build:
            build.return_value.open.return_value = response
            self.assertEqual(check_health(), 0)
            build.return_value.open.assert_called_once_with("http://127.0.0.1:9000/health", timeout=1)

    def test_missing_process_fails_after_bounded_retries_without_http(self):
        with patch("check_service_health.subprocess.check_output", return_value="0\n"), \
             patch("check_service_health.time.sleep"), patch("check_service_health.build_opener") as build, \
             patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(check_health(), 1)
            build.return_value.open.assert_not_called()
