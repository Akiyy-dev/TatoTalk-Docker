import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import probe


class Response(io.BytesIO):
    def __init__(self, content, length=None, final_url="https://example.test/app.apk"):
        super().__init__(content)
        self.headers = {} if length is None else {"Content-Length": str(length)}
        self.final_url = final_url

    def geturl(self):
        return self.final_url


class ProbeTests(unittest.TestCase):
    def setUp(self):
        stdout_patch = patch("sys.stdout", new=io.StringIO())
        stdout_patch.start()
        self.addCleanup(stdout_patch.stop)

    def test_parse_actual_package_and_minimum_sdk(self):
        output = "package: name='example.actual.package' versionCode='9' versionName='1.0'\nsdkVersion:'24'\n"
        self.assertEqual(probe.parse_badging(output), ("example.actual.package", 24))

    def test_parse_badging_rejects_missing_package(self):
        with self.assertRaises(probe.ProbeError):
            probe.parse_badging("sdkVersion:'24'\n")

    def test_device_selection_rejects_multiple_online_devices(self):
        with self.assertRaisesRegex(probe.ProbeError, "exactly one"):
            probe.select_device("List of devices attached\nemulator-5554\tdevice\nemulator-5556\tdevice\n")

    def test_offline_device_cannot_be_selected(self):
        with self.assertRaisesRegex(probe.ProbeError, "offline"):
            probe.select_device("List of devices attached\nemulator-5554\toffline\n")

    def test_selection_returns_online_serial(self):
        self.assertEqual(probe.select_device("List of devices attached\nemulator-5554\tdevice product:sdk\n"), "emulator-5554")

    def test_native_bridge_property_does_not_override_supported_abis(self):
        with self.assertRaisesRegex(probe.ProbeError, "arm64-v8a"):
            probe.validate_compatibility(24, {"arm64-v8a"}, 30, ["x86_64", "x86"])

    def test_guest_too_old_is_rejected(self):
        with self.assertRaisesRegex(probe.ProbeError, "SDK"):
            probe.validate_compatibility(31, set(), 30, ["x86_64"])

    def test_guest_advertising_arm64_accepts_arm64_apk(self):
        probe.validate_compatibility(24, {"arm64-v8a"}, 30, ["x86_64", "arm64-v8a"])

    def test_apk_native_abis_are_read_from_zip(self):
        with tempfile.TemporaryDirectory() as folder:
            apk = Path(folder) / "app.apk"
            with zipfile.ZipFile(apk, "w") as output:
                output.writestr("lib/arm64-v8a/libexample.so", b"example")
                output.writestr("assets/lib/x86/fake.so", b"not-native")
            self.assertEqual(probe.apk_native_abis(apk), {"arm64-v8a"})

    def test_checksum_failure_preserves_old_apk_and_cleans_partial_download(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "app.apk"
            target.write_bytes(b"old")
            with patch.object(probe.urllib.request.OpenerDirector, "open", return_value=Response(b"corrupt")):
                with self.assertRaisesRegex(probe.ProbeError, "SHA256"):
                    probe.download_apk("https://example.test/app.apk", target, "0" * 64)
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(sorted(item.name for item in Path(folder).iterdir()), ["app.apk"])

    def test_size_limit_cleans_partial_download(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "app.apk"
            with patch.object(probe.urllib.request.OpenerDirector, "open", return_value=Response(b"12345")):
                with self.assertRaisesRegex(probe.ProbeError, "size"):
                    probe.download_apk("https://example.test/app.apk", target, max_bytes=4)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_https_is_required_before_network_access(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(probe.ProbeError, "HTTPS"):
                probe.download_apk("http://example.test/app.apk", Path(folder) / "app.apk")

    def test_downgraded_redirect_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(probe.urllib.request.OpenerDirector, "open", return_value=Response(b"apk", final_url="http://example.test/app.apk")):
                with self.assertRaisesRegex(probe.ProbeError, "HTTPS"):
                    probe.download_apk("https://example.test/app.apk", Path(folder) / "app.apk")
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_successful_download_is_atomic_and_hash_is_returned(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "app.apk"
            with patch.object(probe.urllib.request.OpenerDirector, "open", return_value=Response(b"abc", length=3)):
                result = probe.download_apk("https://example.test/app.apk", target)
            self.assertEqual(result, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
            self.assertEqual(target.read_bytes(), b"abc")
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_install_failure_does_not_write_successful_state(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            apk = cache / "TatoTalk.apk"
            with zipfile.ZipFile(apk, "w") as output:
                output.writestr("lib/arm64-v8a/libexample.so", b"example")
            guest = {"serial": "emulator-5554", "sdk": 30, "abis": ["x86_64", "arm64-v8a"], "native_bridge": "libndk_translation.so"}
            with patch.object(probe, "check_guest", return_value=guest), patch.object(probe, "download_apk", return_value="abc"), patch.object(probe, "run", side_effect=["package: name='example.actual'\nsdkVersion:'24'", "Failure [INSTALL_FAILED_INTERNAL_ERROR]"]):
                with self.assertRaisesRegex(probe.ProbeError, "INSTALL_FAILED"):
                    probe.install(cache, "https://example.test/app.apk", None, 1)
            self.assertFalse((cache / "install.json").exists())

    def test_launch_crash_is_detected_after_install_state_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            apk = cache / "TatoTalk.apk"
            with zipfile.ZipFile(apk, "w") as output:
                output.writestr("lib/arm64-v8a/libexample.so", b"example")
            guest = {"serial": "emulator-5554", "sdk": 30, "abis": ["x86_64", "arm64-v8a"], "native_bridge": "libndk_translation.so"}
            outputs = ["package: name='example.actual'\nsdkVersion:'24'", "Success", "Events injected: 1", ""]
            with patch.object(probe, "check_guest", return_value=guest), patch.object(probe, "download_apk", return_value="abc"), patch.object(probe, "run", side_effect=outputs), patch.object(probe.time, "sleep"):
                with self.assertRaisesRegex(probe.ProbeError, "running process"):
                    probe.install(cache, "https://example.test/app.apk", None, 1)
            self.assertEqual(json.loads((cache / "install.json").read_text())["package"], "example.actual")

    def test_install_uses_actual_package_and_saves_only_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            apk = cache / "TatoTalk.apk"
            with zipfile.ZipFile(apk, "w") as output:
                output.writestr("classes.dex", b"example")
            guest = {"serial": "emulator-5554", "sdk": 30, "abis": ["x86_64"], "native_bridge": ""}
            outputs = ["package: name='example.actual'\nsdkVersion:'24'", "Success", "Events injected: 1", "4321"]
            with patch.object(probe, "check_guest", return_value=guest), patch.object(probe, "download_apk", return_value="abc"), patch.object(probe, "run", side_effect=outputs), patch.object(probe.time, "sleep"):
                state = probe.install(cache, "https://example.test/app.apk", None, 1)
            self.assertEqual(state["package"], "example.actual")
            self.assertEqual(set(json.loads((cache / "install.json").read_text())), {"package", "sha256", "min_sdk", "native_abis"})


    def test_guest_check_uses_configurable_expected_abi(self):
        outputs = ["List of devices attached\nemulator-5554\tdevice\n", "1", "30", "x86_64,x86", "libndk_translation.so"]
        with patch.object(probe, "check_acceleration"), patch.object(probe, "run", side_effect=outputs), patch.dict(probe.os.environ, {"TATOTALK_EXPECTED_ABI": "x86_64"}):
            guest = probe.check_guest(10)
        self.assertEqual(guest["abis"], ["x86_64", "x86"])

    def test_guest_boot_timeout_reports_offline_state(self):
        ticks = iter([0, 0, 0, 2, 2])
        with patch.object(probe, "check_acceleration"), patch.object(probe, "run", return_value="List of devices attached\nemulator-5554\toffline\n"), patch.object(probe.time, "monotonic", side_effect=lambda: next(ticks)), patch.object(probe.time, "sleep"):
            with self.assertRaisesRegex(probe.ProbeError, "offline"):
                probe.check_guest(1)

    def test_kvm_command_failure_is_actionable(self):
        failure = subprocess.CompletedProcess(["emulator", "-accel-check"], 1, "KVM not usable", "")
        with patch.object(probe.shutil, "which", return_value="emulator"), patch.object(probe.subprocess, "run", return_value=failure):
            with self.assertRaisesRegex(probe.ProbeError, "KVM"):
                probe.check_acceleration()

    def test_status_without_installation_reports_empty_package(self):
        outputs = ["List of devices attached\nemulator-5554\tdevice\n", "1"]
        with tempfile.TemporaryDirectory() as folder, patch.object(probe, "run", side_effect=outputs):
            report = probe.status(Path(folder))
        self.assertEqual(report, {"serial": "emulator-5554", "boot_completed": True, "package": None, "installed": False, "running": False})

    def test_status_handles_broken_metadata_without_traceback(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            (cache / "install.json").write_text("not-json")
            with patch.object(probe, "run", side_effect=["List of devices attached\nemulator-5554\tdevice\n", "1"]):
                with self.assertRaisesRegex(probe.ProbeError, "metadata"):
                    probe.status(cache)

    def _boot_success_responses(self):
        return [subprocess.CompletedProcess(["adb"], 0, text, "") for text in [
            "List of devices attached\nemulator-5554\tdevice\n", "1", "30", "x86_64,arm64-v8a", "libndk_translation.so"
        ]]

    def test_boot_retries_temporary_adb_command_failure(self):
        responses = [subprocess.CompletedProcess(["adb"], 1, "", "cannot connect to daemon")] + self._boot_success_responses()
        with patch.object(probe, "check_acceleration"), patch.object(probe.subprocess, "run", side_effect=responses), patch.object(probe.time, "sleep"):
            guest = probe.check_guest(10)
        self.assertEqual(guest["sdk"], 30)

    def test_boot_retries_temporary_adb_command_timeout(self):
        responses = [subprocess.TimeoutExpired(["adb", "devices"], 1)] + self._boot_success_responses()
        with patch.object(probe, "check_acceleration"), patch.object(probe.subprocess, "run", side_effect=responses), patch.object(probe.time, "sleep"):
            guest = probe.check_guest(10)
        self.assertEqual(guest["serial"], "emulator-5554")

    def test_boot_retries_device_going_offline_during_getprop(self):
        responses = [subprocess.CompletedProcess(["adb"], 0, "List of devices attached\nemulator-5554\tdevice\n", ""),
                     subprocess.CompletedProcess(["adb"], 1, "", "error: device offline")] + self._boot_success_responses()
        with patch.object(probe, "check_acceleration"), patch.object(probe.subprocess, "run", side_effect=responses), patch.object(probe.time, "sleep"):
            guest = probe.check_guest(10)
        self.assertIn("arm64-v8a", guest["abis"])

    def test_boot_missing_adb_fails_immediately(self):
        with patch.object(probe, "check_acceleration"), patch.object(probe.subprocess, "run", side_effect=FileNotFoundError("adb unavailable")):
            with self.assertRaisesRegex(probe.ProbeError, "Required executable"):
                probe.check_guest(10)

    def test_boot_multiple_online_devices_fails_immediately(self):
        response = subprocess.CompletedProcess(["adb"], 0, "List of devices attached\nemulator-5554\tdevice\nemulator-5556\tdevice\n", "")
        with patch.object(probe, "check_acceleration"), patch.object(probe.subprocess, "run", return_value=response):
            with self.assertRaisesRegex(probe.ProbeError, "exactly one"):
                probe.check_guest(10)

    def test_redirect_rejects_intermediate_http_hop_even_if_final_would_be_https(self):
        handler = probe.HttpsOnlyRedirectHandler()
        start = probe.urllib.request.Request("https://example.test/app.apk")
        secure_hop = handler.redirect_request(start, None, 302, "Found", {}, "https://mirror.test/app.apk")
        with self.assertRaisesRegex(probe.ProbeError, "HTTPS"):
            handler.redirect_request(secure_hop, None, 302, "Found", {}, "http://mirror.test/redirect-back-to-https")

    def test_redirect_rejects_embedded_credentials(self):
        handler = probe.HttpsOnlyRedirectHandler()
        request = probe.urllib.request.Request("https://example.test/app.apk")
        with self.assertRaisesRegex(probe.ProbeError, "credentials"):
            handler.redirect_request(request, None, 302, "Found", {}, "https://user:password@mirror.test/app.apk")

    def test_redirect_allows_https_only_target(self):
        handler = probe.HttpsOnlyRedirectHandler()
        request = probe.urllib.request.Request("https://example.test/app.apk")
        redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://mirror.test/app.apk")
        self.assertEqual(redirected.full_url, "https://mirror.test/app.apk")

    def test_download_blocks_intermediate_http_redirect_and_keeps_old_cache(self):
        from email.message import Message
        from urllib.response import addinfourl

        visited = []
        responses = []

        class Transport(probe.urllib.request.BaseHandler):
            handler_order = 100

            def https_open(self, request):
                visited.append(request.full_url)
                headers = Message()
                headers["Location"] = "http://mirror.test/redirect-back-to-https"
                response = addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
                response.msg = "Found"
                responses.append(response)
                return response

            def http_open(self, request):
                raise AssertionError("An insecure HTTP hop must never be requested")

        build_opener = probe.urllib.request.build_opener
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "app.apk"
            target.write_bytes(b"previous verified APK")
            with patch.object(probe.urllib.request, "build_opener", side_effect=lambda *handlers: build_opener(*handlers, Transport())):
                with self.assertRaisesRegex(probe.ProbeError, "HTTPS"):
                    probe.download_apk("https://example.test/app.apk", target)
            self.assertEqual(visited, ["https://example.test/app.apk"])
            self.assertTrue(responses[0].closed)
            self.assertEqual(target.read_bytes(), b"previous verified APK")
            self.assertEqual([item.name for item in Path(folder).iterdir()], ["app.apk"])

    def test_boot_timeout_retains_last_transient_adb_error(self):
        ticks = iter([0, 0, 0, 2, 2])
        failed = subprocess.CompletedProcess(["adb"], 1, "", "daemon temporarily unavailable")
        with patch.object(probe, "check_acceleration"), patch.object(probe.subprocess, "run", return_value=failed), patch.object(probe.time, "monotonic", side_effect=lambda: next(ticks)), patch.object(probe.time, "sleep"):
            with self.assertRaisesRegex(probe.ProbeError, "boot timed out.*daemon temporarily unavailable"):
                probe.check_guest(1)


if __name__ == "__main__":
    unittest.main()
