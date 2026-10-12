"""Exercise the publisher through Bash with a stateful fake GitHub API."""

import hashlib
import io
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import package_release

FAKE_GH = r'''
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

sys.stdout.reconfigure(newline="\n")
state_path = Path(os.environ["TATOTALK_FAKE_STATE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]
state["calls"].append(args)

def finish(value=None, code=0):
    state_path.write_text(json.dumps(state), encoding="utf-8")
    if value is not None:
        print(json.dumps(value))
    raise SystemExit(code)

def release():
    return {
        "id": 41,
        "tag_name": "v0.1.1",
        "draft": state["draft"],
        "upload_url": "https://uploads.github.com/repos/Akiyy-dev/TatoTalk-Docker/releases/41/assets{?name,label}",
        "html_url": "https://github.com/Akiyy-dev/TatoTalk-Docker/releases/tag/v0.1.1",
    }

# Allow the old creation command only to reproduce its immediate-list failure.
if args[:2] == ["release", "create"]:
    state["exists"] = True
    finish()
if not args or args[0] != "api":
    finish(code=2)
method, input_file, headers, endpoint = "GET", None, [], None
index = 1
while index < len(args):
    argument = args[index]
    if argument in ("--method", "-X", "--input", "--header", "-H"):
        value = args[index + 1]
        if argument in ("--method", "-X"):
            method = value
        elif argument == "--input":
            input_file = value
        else:
            headers.append(value)
        index += 2
    elif argument in ("--paginate", "--slurp"):
        index += 1
    elif argument.startswith("repos/") or argument.startswith("https://"):
        endpoint = argument
        index += 1
    else:
        finish(code=2)
if endpoint is None:
    finish(code=2)
scenario = state["scenario"]
if "/git/ref/tags/" in endpoint:
    if scenario == "missing_tag":
        finish(code=1)
    finish({"object": {
        "type": "tag" if scenario == "annotated" else "commit",
        "sha": ("0" if scenario == "wrong_tag" else "a") * 40,
    }})
if "/git/tags/" in endpoint:
    finish({"object": {"type": "commit", "sha": "a" * 40}})
if endpoint.endswith("/releases?per_page=100"):
    # A newly created release never becomes visible in this list.
    finish([[release()]] if state["initial_exists"] else [[]])
if method == "POST" and endpoint.endswith("/releases"):
    payload = json.loads(Path(input_file).read_text(encoding="utf-8"))
    if payload.get("tag_name") != "v0.1.1" or payload.get("draft") is not True:
        finish(code=2)
    if payload.get("target_commitish") != "a" * 40:
        finish(code=2)
    state["exists"] = True
    response = release()
    if scenario == "created_wrong_tag":
        response["tag_name"] = "v9.9.9"
    if scenario == "unsafe_upload_url":
        response["upload_url"] = "https://example.invalid/assets{?name,label}"
    finish(response)
if endpoint.endswith("/releases/41"):
    response = release()
    if method == "GET":
        state["id_reads"] += 1
        if scenario == "wrong_id":
            response["id"] = 42
        if scenario == "wrong_release_tag":
            response["tag_name"] = "v9.9.9"
        if scenario == "published_during_upload" and state["attempts"]:
            state["draft"] = False
            response["draft"] = False
        finish(response)
    if method == "PATCH":
        payload = json.loads(Path(input_file).read_text(encoding="utf-8"))
        if payload.get("tag_name") != "v0.1.1":
            finish(code=2)
        state["patches"].append(payload)
        if payload.get("draft") is False:
            state["draft"] = False
        finish(release())
if endpoint.endswith("/releases/41/assets?per_page=100"):
    assets = list(state["assets"])
    if assets:
        if scenario == "bad_digest":
            assets[0] = dict(assets[0], digest="sha256:" + "0" * 64)
        if scenario == "missing_digest":
            assets[0] = dict(assets[0])
            assets[0].pop("digest", None)
        if scenario == "bad_size":
            assets[0] = dict(assets[0], size=assets[0]["size"] + 1)
        if scenario == "bad_state":
            assets[0] = dict(assets[0], state="starter")
        if scenario == "duplicate_asset":
            assets.append(dict(assets[0]))
    finish([assets])
if method == "DELETE" and "/releases/assets/" in endpoint:
    asset_id = int(endpoint.rsplit("/", 1)[1])
    if not any(asset["id"] == asset_id for asset in state["assets"]):
        finish(code=2)
    state["deletions"].append(asset_id)
    state["assets"] = [asset for asset in state["assets"] if asset["id"] != asset_id]
    finish()
if method == "POST" and endpoint.startswith("https://uploads.github.com/"):
    parsed = urlsplit(endpoint)
    if parsed.path != "/repos/Akiyy-dev/TatoTalk-Docker/releases/41/assets":
        finish(code=2)
    name = parse_qs(parsed.query).get("name", [None])[0]
    if input_file is None or "Content-Type: application/octet-stream" not in headers:
        finish(code=2)
    path = Path(input_file)
    if name != path.name or any(asset["name"] == name for asset in state["assets"]):
        finish(code=2)
    attempt = state["attempts"].get(name, 0) + 1
    state["attempts"][name] = attempt
    if scenario == "retries_exhausted" or (scenario == "retries" and attempt < 3):
        finish(code=1)
    state["next_asset_id"] += 1
    content = path.read_bytes()
    asset = {
        "id": state["next_asset_id"],
        "name": name,
        "size": len(content),
        "state": "uploaded",
        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
    }
    state["assets"].append(asset)
    # The server stored the upload, but the client lost the success response.
    if scenario == "accepted_then_failed" and attempt == 1:
        finish(code=1)
    finish(asset)
finish(code=2)
'''


class PublishReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shell = (
            os.environ.get("TATOTALK_TEST_BASH")
            or os.environ.get("TATOTALK_TEST_SHELL")
            or shutil.which("bash")
        )
        if not cls.shell:
            raise unittest.SkipTest("Bash is required for release publisher tests")
        cls.script = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "scripts"
            / "publish-release.sh"
        )

    def run_case(self, scenario="fresh"):
        with tempfile.TemporaryDirectory(prefix="release fixture ") as directory:
            root = Path(directory)
            dist = root / "dist with spaces"
            dist.mkdir()
            (dist / "TatoTalk-Docker.zip").write_bytes(b"deployment archive fixture")
            image = root / "image.tar"
            content = random.Random(1234).randbytes(1200)
            with tarfile.open(image, "w") as archive:
                entry = tarfile.TarInfo("image-layer.bin")
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
            package_release.package_release(image, dist, "v0.1.1", part_size=1000)
            expected = {
                path.name: {
                    "size": path.stat().st_size,
                    "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in dist.iterdir()
            }
            notes = root / "notes.txt"
            notes.write_text("Docker image and deployment bundle.\n", encoding="utf-8")
            summary = root / "summary.txt"
            summary.write_text("", encoding="utf-8")
            initial_exists = scenario in {"draft_stale", "published", "stranger"}
            state = {
                "scenario": scenario,
                "initial_exists": initial_exists,
                "exists": initial_exists,
                "draft": scenario != "published",
                "assets": [],
                "attempts": {},
                "calls": [],
                "patches": [],
                "deletions": [],
                "id_reads": 0,
                "next_asset_id": 100,
            }
            if scenario == "draft_stale":
                state["assets"] = [{
                    "id": 91,
                    "name": "tatotalk-android-amd64.tar.gz.part-9999",
                    "size": 3,
                    "state": "uploaded",
                    "digest": "sha256:" + "b" * 64,
                }]
            if scenario == "stranger":
                state["assets"] = [{
                    "id": 92,
                    "name": "unrelated.zip",
                    "size": 3,
                    "state": "uploaded",
                    "digest": "sha256:" + "b" * 64,
                }]
            state_path = root / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            fake = root / "fake_gh.py"
            fake.write_text(FAKE_GH, encoding="utf-8", newline="\n")
            binary_directory = root / "bin"
            binary_directory.mkdir()
            wrappers = {
                "gh": '#!/usr/bin/env bash\nexec "$TATOTALK_TEST_PYTHON" "$TATOTALK_TEST_FAKE_GH" "$@"\n',
                "python3": '#!/usr/bin/env bash\nexec "$TATOTALK_TEST_PYTHON" "$@"\n',
            }
            for name, wrapper in wrappers.items():
                path = binary_directory / name
                path.write_text(wrapper, encoding="utf-8", newline="\n")
                path.chmod(0o755)
            bash_env = root / "bash_env.sh"
            bash_env.write_text(
                'if command -v cygpath >/dev/null 2>&1; then\n'
                '  fixture_bin="$(cygpath -u "$TATOTALK_TEST_BIN")"\n'
                'else\n'
                '  fixture_bin="$TATOTALK_TEST_BIN"\n'
                'fi\n'
                'export PATH="$fixture_bin:$PATH"\nsleep() { :; }\n',
                encoding="utf-8",
                newline="\n",
            )
            env = dict(
                os.environ,
                RELEASE_VERSION="v0.1.1",
                GH_REPO="Akiyy-dev/TatoTalk-Docker",
                GH_TOKEN="fake-secret-never-log",
                TATOTALK_FAKE_STATE=state_path.as_posix(),
                TATOTALK_TEST_FAKE_GH=fake.as_posix(),
                TATOTALK_TEST_PYTHON=Path(sys.executable).as_posix(),
                TATOTALK_TEST_BIN=binary_directory.as_posix(),
                RUNNER_TEMP=root.as_posix(),
                GITHUB_STEP_SUMMARY=summary.as_posix(),
                BASH_ENV=bash_env.as_posix(),
            )
            result = subprocess.run(
                [
                    self.shell,
                    self.script.as_posix(),
                    dist.as_posix(),
                    notes.as_posix(),
                    "a" * 40,
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            summary_text = summary.read_text(encoding="utf-8")
            self.assertNotIn("fake-secret-never-log", result.stdout + result.stderr)
            return result, state, summary_text, expected

    def test_created_draft_publishes_even_when_list_stays_empty(self):
        result, state, summary, expected = self.run_case()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(state["exists"])
        self.assertFalse(state["draft"])
        self.assertEqual(
            {asset["name"]: {"size": asset["size"], "digest": asset["digest"]}
             for asset in state["assets"]},
            expected,
        )
        self.assertIn("/releases/tag/v0.1.1", summary)
        self.assertGreater(state["id_reads"], 1)
        self.assertEqual(
            sum("repos/Akiyy-dev/TatoTalk-Docker/releases?per_page=100" in call
                for call in state["calls"]),
            1,
        )
        self.assertTrue(all(call[0] == "api" for call in state["calls"]))
        self.assertTrue(all(patch["tag_name"] == "v0.1.1" for patch in state["patches"]))

    def test_existing_draft_deletes_stale_part_by_id(self):
        result, state, _, _ = self.run_case("draft_stale")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(state["draft"])
        self.assertIn(91, state["deletions"])
        self.assertNotIn(
            "tatotalk-android-amd64.tar.gz.part-9999",
            [asset["name"] for asset in state["assets"]],
        )

    def test_published_release_cannot_be_overwritten(self):
        result, state, _, _ = self.run_case("published")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(state["draft"])
        self.assertFalse(state["patches"])
        self.assertFalse(state["attempts"])

    def test_unknown_draft_asset_is_preserved(self):
        result, state, _, _ = self.run_case("stranger")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(state["draft"])
        self.assertEqual([asset["name"] for asset in state["assets"]], ["unrelated.zip"])
        self.assertFalse(state["deletions"])
        self.assertFalse(state["patches"])
        self.assertFalse(state["attempts"])

    def test_upload_success_with_lost_response_is_replaced_by_id_on_retry(self):
        result, state, _, expected = self.run_case("accepted_then_failed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(state["draft"])
        self.assertEqual(set(state["attempts"].values()), {2})
        self.assertEqual(len(state["deletions"]), len(expected))
        self.assertEqual(len(state["assets"]), len(expected))

    def test_transient_upload_failure_retries_three_times(self):
        result, state, _, _ = self.run_case("retries")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(state["draft"])
        self.assertEqual(set(state["attempts"].values()), {3})

    def test_three_failed_uploads_preserve_draft(self):
        result, state, _, _ = self.run_case("retries_exhausted")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(state["draft"])
        self.assertEqual(list(state["attempts"].values()), [3])
        self.assertTrue(all(patch.get("draft") is not False for patch in state["patches"]))

    def test_wrong_release_identity_or_upload_host_is_rejected(self):
        for scenario in ("created_wrong_tag", "wrong_id", "wrong_release_tag", "unsafe_upload_url"):
            with self.subTest(scenario=scenario):
                result, state, _, _ = self.run_case(scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(state["draft"])
                self.assertFalse(state["patches"])
                self.assertFalse(state["attempts"])

    def test_remote_integrity_failure_preserves_draft(self):
        for scenario in ("bad_digest", "missing_digest", "bad_size", "bad_state", "duplicate_asset"):
            with self.subTest(scenario=scenario):
                result, state, _, _ = self.run_case(scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(state["draft"])
                self.assertTrue(all(patch.get("draft") is not False for patch in state["patches"]))

    def test_publication_during_upload_is_rejected(self):
        result, state, _, _ = self.run_case("published_during_upload")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(state["draft"])
        self.assertEqual(len(state["attempts"]), 1)
        self.assertTrue(all(patch.get("draft") is not False for patch in state["patches"]))

    def test_missing_or_mismatched_source_tag_creates_nothing(self):
        for scenario in ("missing_tag", "wrong_tag"):
            with self.subTest(scenario=scenario):
                result, state, _, _ = self.run_case(scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(state["exists"])
                self.assertFalse(state["attempts"])

    def test_annotated_tag_resolves_to_source_commit(self):
        result, state, _, _ = self.run_case("annotated")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(state["draft"])


if __name__ == "__main__":
    unittest.main()
