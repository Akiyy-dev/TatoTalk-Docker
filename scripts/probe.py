#!/usr/bin/env python3
"""Verify an Android emulator and install TatoTalk without reading account data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_APK_URL = "https://downtato.ffvjei.com/android/TatoTalk-26100306.apk"
MAX_APK_BYTES = 200 * 1024 * 1024


class ProbeError(Exception):
    """An actionable deployment verification error."""


class ExecutableMissingError(ProbeError):
    """A required program is missing; waiting cannot fix it."""


class CommandError(ProbeError):
    """A command failed or timed out; ADB may recover during boot."""


class DeviceNotReadyError(ProbeError):
    """No online Android device is ready yet."""


class MultipleDevicesError(ProbeError):
    """More than one online guest makes choosing a device unsafe."""


def validate_https_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ProbeError("APK URL and every redirect must be HTTPS without embedded credentials.")


class HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject unsafe redirects before urllib can request the next hop."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        try:
            validate_https_url(target)
        except ProbeError:
            if fp is not None:
                fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, target)


def run(args: list[str], timeout: float = 30, allow_failure: bool = False) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=max(0.001, timeout), check=False)
    except FileNotFoundError as exc:
        raise ExecutableMissingError(f"Required executable not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"Command timed out: {args[0]}") from exc
    if result.returncode and not allow_failure:
        detail = (result.stderr or result.stdout).strip()[:1500]
        raise CommandError(f"{args[0]} failed (exit {result.returncode}): {detail}")
    return result.stdout.strip()


def parse_badging(output: str) -> tuple[str, int]:
    package = re.search(r"^package: name='([A-Za-z][A-Za-z0-9_.]*)'", output, re.MULTILINE)
    minimum = re.search(r"^sdkVersion:'(\d+)'", output, re.MULTILINE)
    if package is None or minimum is None:
        raise ProbeError("aapt did not return a valid package name and minimum SDK.")
    return package.group(1), int(minimum.group(1))


def apk_native_abis(apk: Path) -> set[str]:
    try:
        with zipfile.ZipFile(apk) as archive:
            return {name.split("/")[1] for name in archive.namelist() if re.match(r"^lib/[^/]+/[^/]+\.so$", name)}
    except (OSError, zipfile.BadZipFile) as exc:
        raise ProbeError("Downloaded file is not a readable APK ZIP archive.") from exc


def validate_compatibility(min_sdk: int, apk_abis: set[str], guest_sdk: int, guest_abis: list[str]) -> None:
    if guest_sdk < min_sdk:
        raise ProbeError(f"APK requires SDK {min_sdk}; guest has SDK {guest_sdk}.")
    if apk_abis and not apk_abis.intersection(guest_abis):
        raise ProbeError(
            f"Unsupported APK native ABI: {', '.join(sorted(apk_abis))}; "
            f"guest supports: {', '.join(guest_abis) or '(none)'}. "
            "A native-bridge property alone does not prove ARM translation works. "
            "Use a guest image that advertises the required ABI; do not force installation."
        )


def select_device(output: str) -> str:
    devices = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] not in {"List", "*"}:
            devices.append((fields[0], fields[1]))
    online = [serial for serial, status in devices if status == "device"]
    if len(online) != 1:
        states = ", ".join(f"{serial}={status}" for serial, status in devices) or "none"
        error = MultipleDevicesError if online else DeviceNotReadyError
        raise error(f"Expected exactly one online ADB device; found {len(online)} ({states}).")
    return online[0]


def adb(serial: str, *args: str, timeout: float = 30, allow_failure: bool = False) -> str:
    return run(["adb", "-s", serial, *args], timeout=timeout, allow_failure=allow_failure)


def check_acceleration() -> None:
    executable = shutil.which("emulator")
    if executable is None:
        for key in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
            root = os.environ.get(key)
            candidate = Path(root) / "emulator" / "emulator" if root else None
            if candidate and candidate.is_file():
                executable = str(candidate)
                break
    if executable is None:
        raise ProbeError("Android emulator executable not found in PATH or Android SDK directory.")
    try:
        result = subprocess.run([executable, "-accel-check"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError("Could not run emulator -accel-check.") from exc
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()[:1500]
        raise ProbeError(f"KVM acceleration check failed: {detail}. Check /dev/kvm mapping and permissions.")
    print("KVM acceleration check: passed")


def check_guest(timeout: int, require_arm64: bool = True) -> dict:
    if timeout <= 0:
        raise ProbeError("Boot timeout must be positive.")
    check_acceleration()
    deadline = time.monotonic() + timeout
    latest = "No ADB device yet."
    serial = None
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            output = run(["adb", "devices", "-l"], timeout=min(15, remaining))
            serial = select_device(output)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CommandError("Android boot wait budget exhausted.")
            booted = adb(serial, "shell", "getprop", "sys.boot_completed", timeout=min(15, remaining))
            if booted == "1":
                break
            latest = "ADB device present, but Android has not completed boot."
        except (DeviceNotReadyError, CommandError) as exc:
            latest = str(exc)
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    else:
        raise ProbeError(f"Android boot timed out after {timeout}s: {latest}")
    sdk_text = adb(serial, "shell", "getprop", "ro.build.version.sdk")
    if not sdk_text.isdigit():
        raise ProbeError("Guest did not report a numeric Android SDK version.")
    abi_text = adb(serial, "shell", "getprop", "ro.product.cpu.abilist")
    abis = [value.strip() for value in abi_text.split(",") if value.strip()]
    bridge = adb(serial, "shell", "getprop", "ro.dalvik.vm.native.bridge")
    guest = {"serial": serial, "sdk": int(sdk_text), "abis": abis, "native_bridge": bridge}
    print(json.dumps(guest, ensure_ascii=False))
    if require_arm64:
        expected_abi = os.environ.get("TATOTALK_EXPECTED_ABI", "arm64-v8a").strip()
        if not expected_abi:
            raise ProbeError("TATOTALK_EXPECTED_ABI must not be empty.")
        validate_compatibility(1, {expected_abi}, guest["sdk"], abis)
    return guest


def download_apk(url: str, target: Path, sha256: str | None = None, max_bytes: int = MAX_APK_BYTES) -> str:
    validate_https_url(url)
    expected = sha256.strip().lower() if sha256 else None
    if expected and re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise ProbeError("TATOTALK_APK_SHA256 must contain exactly 64 hexadecimal characters.")
    if max_bytes <= 0:
        raise ProbeError("APK size limit must be positive.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "TatoTalk-Docker-Probe/1.0"})
    try:
        opener = urllib.request.build_opener(HttpsOnlyRedirectHandler())
        with opener.open(request, timeout=30) as response:
            validate_https_url(response.geturl())
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise ProbeError(f"APK exceeds download size limit ({max_bytes} bytes).")
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix="apk-", suffix=".part", delete=False) as output:
                temporary = Path(output.name)
                received = 0
                while True:
                    block = response.read(128 * 1024)
                    if not block:
                        break
                    received += len(block)
                    if received > max_bytes:
                        raise ProbeError(f"APK exceeds download size limit ({max_bytes} bytes).")
                    output.write(block)
                    digest.update(block)
                if not received:
                    raise ProbeError("APK download was empty.")
                if length and length.isdigit() and received != int(length):
                    raise ProbeError("APK download size differs from Content-Length; retry installation.")
                output.flush()
                os.fsync(output.fileno())
        actual = digest.hexdigest()
        if expected and actual != expected:
            raise ProbeError(f"APK SHA256 mismatch: expected {expected}, received {actual}.")
        os.replace(temporary, target)
        temporary = None
        return actual
    except (urllib.error.URLError, OSError) as exc:
        raise ProbeError(f"APK download failed: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_state(cache: Path, state: dict) -> None:
    temporary = cache / "install.json.tmp"
    try:
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, cache / "install.json")
    finally:
        temporary.unlink(missing_ok=True)


def install(cache: Path, url: str, checksum: str | None, timeout: int) -> dict:
    guest = check_guest(timeout, require_arm64=False)
    apk = cache / "TatoTalk.apk"
    digest = download_apk(url, apk, checksum)
    print(f"APK SHA256: {digest}")
    native_abis = apk_native_abis(apk)
    package, min_sdk = parse_badging(run(["aapt", "dump", "badging", str(apk)]))
    validate_compatibility(min_sdk, native_abis, guest["sdk"], guest["abis"])
    print(f"APK package: {package}; minimum SDK: {min_sdk}; native ABIs: {', '.join(sorted(native_abis)) or '(none)'}")
    result = adb(guest["serial"], "install", "-r", str(apk), timeout=180)
    if not any(line.strip() == "Success" for line in result.splitlines()):
        raise ProbeError(f"APK installation did not succeed: {result[:1500]}")
    state = {"package": package, "sha256": digest, "min_sdk": min_sdk, "native_abis": sorted(native_abis)}
    save_state(cache, state)
    adb(guest["serial"], "shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(3)
    pid = adb(guest["serial"], "shell", "pidof", package, allow_failure=True)
    if not re.fullmatch(r"\d+(?:\s+\d+)*", pid):
        raise ProbeError("App installed, but no running process was found after launch. Inspect the remote desktop for a crash; ARM translation compatibility is not proven.")
    print("App installation and initial process startup passed. Complete login manually in noVNC; this check does not prove account login or message reception.")
    return state


def status(cache: Path) -> dict:
    serial = select_device(run(["adb", "devices", "-l"]))
    report = {"serial": serial, "boot_completed": adb(serial, "shell", "getprop", "sys.boot_completed") == "1"}
    state_path = cache / "install.json"
    if not state_path.is_file():
        report.update({"package": None, "installed": False, "running": False})
    else:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            package = state["package"]
            if not isinstance(package, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]*", package) is None:
                raise ValueError("invalid package name")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ProbeError("Saved installation metadata is invalid; run install again.") from exc
        installed = adb(serial, "shell", "pm", "path", package, allow_failure=True).startswith("package:")
        pid = adb(serial, "shell", "pidof", package, allow_failure=True) if installed else ""
        report.update({"package": package, "installed": installed, "running": bool(re.fullmatch(r"\d+(?:\s+\d+)*", pid))})
    print(json.dumps(report, ensure_ascii=False))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "install", "status"))
    parser.add_argument("--timeout", type=int, default=None, help="Android boot wait, default TATOTALK_BOOT_TIMEOUT or 300 seconds")
    args = parser.parse_args(argv)
    cache = Path(os.environ.get("TATOTALK_STATE_DIR", "/home/androidusr/.tatotalk"))
    try:
        timeout = args.timeout if args.timeout is not None else int(os.environ.get("TATOTALK_BOOT_TIMEOUT", "300"))
        if args.command == "check":
            check_guest(timeout)
        elif args.command == "install":
            install(cache, os.environ.get("TATOTALK_APK_URL", DEFAULT_APK_URL), os.environ.get("TATOTALK_APK_SHA256") or None, timeout)
        else:
            status(cache)
    except (ProbeError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
