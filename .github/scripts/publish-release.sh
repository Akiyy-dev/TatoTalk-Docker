#!/usr/bin/env bash
# Publish only after GitHub confirms every uploaded artifact's SHA-256.
set +x
set -euo pipefail

fail() {
  printf 'Release publication failed: %s\n' "$*" >&2
  exit 1
}

[[ $# -eq 3 ]] || fail "Usage: publish-release.sh DIST NOTES_FILE SOURCE_COMMIT_SHA"
: "${RELEASE_VERSION:?Set RELEASE_VERSION to an existing release tag}"
: "${GH_REPO:?Set GH_REPO to OWNER/REPOSITORY}"
: "${GH_TOKEN:?Set GH_TOKEN for the release step}"
[[ "$GH_REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "Invalid GH_REPO"
[[ "$RELEASE_VERSION" =~ ^v[0-9][A-Za-z0-9._+-]*$ ]] || fail "Invalid RELEASE_VERSION"
[[ "$3" =~ ^[a-fA-F0-9]{40}$ ]] || fail "SOURCE_COMMIT_SHA must be a full commit SHA"
[[ -d "$1" ]] || fail "Artifact directory does not exist"
[[ -f "$2" ]] || fail "Release notes file does not exist"
source_commit="${3,,}"
dist="$1"
notes_file="$2"

state_dir="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/tatotalk-release.XXXXXX")"
cleanup() {
  rm -f -- "$state_dir"/*.json "$state_dir"/*.txt "$state_dir"/*.nul "$state_dir"/*.py
  rmdir -- "$state_dir"
}
trap cleanup EXIT

cat > "$state_dir/validate.py" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlencode, urlsplit

# Bash consumes these outputs on Linux and in Windows Git Bash.
sys.stdout.reconfigure(newline="\n")
state = Path(sys.argv[1])
command = sys.argv[2]
required = {"TatoTalk-Docker.zip", "image-manifest.json", "SHA256SUMS.txt"}
part_pattern = re.compile(r"tatotalk-android-amd64\.tar\.gz\.part-[0-9]{4}")

def fail(message):
    raise SystemExit("Release validation failed: " + message)

def managed(name):
    return isinstance(name, str) and (name in required or part_pattern.fullmatch(name))

def read_json(filename):
    return json.loads((state / filename).read_text(encoding="utf-8"))

def write_text(filename, content):
    (state / filename).write_text(content, encoding="utf-8", newline="\n")

def pages(filename):
    data = read_json(filename)
    if not isinstance(data, list) or any(not isinstance(page, list) for page in data):
        fail("Invalid paginated GitHub response")
    entries = [entry for page in data for entry in page]
    if any(not isinstance(entry, dict) for entry in entries):
        fail("Invalid GitHub entry")
    return entries

def identity(release, expected_id, draft):
    release_id = release.get("id")
    if type(release_id) is not int or release_id <= 0:
        fail("Invalid release identifier")
    if expected_id and release_id != int(expected_id):
        fail("GitHub returned a different release identifier")
    if release.get("tag_name") != os.environ["RELEASE_VERSION"]:
        fail("GitHub returned a different release tag")
    if release.get("draft") is not draft:
        fail("An already published release cannot be overwritten" if draft else
             "GitHub has not confirmed publication")
    return release_id

if command == "local":
    directory = Path(sys.argv[3]).resolve()
    files = sorted(directory.iterdir(), key=lambda path: path.name)
    if any(not managed(path.name) or not path.is_file() or path.is_symlink() for path in files):
        fail("Artifact directory contains an unexpected file or directory")
    names = {path.name for path in files}
    if not required <= names:
        fail("Required release artifacts are missing")
    parts = sorted(name for name in names if part_pattern.fullmatch(name))
    if not parts or parts != [
        f"tatotalk-android-amd64.tar.gz.part-{number:04d}" for number in range(1, len(parts) + 1)
    ]:
        fail("Image parts must start at 0001 and have no gaps")
    if len(files) > 1000:
        fail("Release exceeds GitHub's 1000-asset limit")
    manifest = {}
    for path in files:
        size = path.stat().st_size
        if size <= 0 or size >= 2 * 1024**3:
            fail(f"{path.name} must be nonempty and smaller than 2 GiB")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest[path.name] = {"size": size, "digest": "sha256:" + digest}
    write_text("local.json", json.dumps(manifest))
    (state / "files.nul").write_bytes(
        b"".join(path.as_posix().encode("utf-8") + b"\0" for path in files)
    )
elif command == "tag":
    obj = read_json("tag.json").get("object", {})
    kind, sha = obj.get("type"), obj.get("sha", "")
    if kind not in {"commit", "tag"} or not re.fullmatch(r"[a-fA-F0-9]{40}", sha):
        fail("Remote tag does not resolve to a commit")
    print(kind, sha.lower())
elif command == "release":
    matches = [
        entry for entry in pages("releases.json")
        if entry.get("tag_name") == os.environ["RELEASE_VERSION"]
    ]
    if not matches:
        print("missing")
    else:
        if len(matches) != 1:
            fail("Multiple releases use the requested tag")
        print(identity(matches[0], "", True))
elif command == "payload":
    tag = os.environ["RELEASE_VERSION"]
    payload = {"tag_name": tag}
    if sys.argv[3] == "publish":
        payload["draft"] = False
    else:
        payload.update({
            "name": f"TatoTalk Docker {tag}",
            "body": Path(sys.argv[4]).read_text(encoding="utf-8"),
        })
        if sys.argv[3] == "create":
            payload.update({"draft": True, "prerelease": False, "target_commitish": sys.argv[5]})
    write_text("payload.json", json.dumps(payload))
elif command == "draft":
    release = read_json("release.json")
    release_id = identity(release, sys.argv[3], True)
    # Only send the token to GitHub's upload endpoint for this repository and ID.
    base = release.get("upload_url", "").split("{", 1)[0]
    parsed = urlsplit(base)
    expected_path = f'/repos/{os.environ["GH_REPO"]}/releases/{release_id}/assets'
    if (parsed.scheme != "https" or parsed.netloc != "uploads.github.com" or
            parsed.path.lower() != expected_path.lower() or parsed.query or parsed.fragment):
        fail("Unexpected release upload URL")
    write_text("upload-base.txt", base)
    print(release_id)
elif command in {"inventory", "replace", "verify"}:
    local = read_json("local.json")
    remote = {}
    for asset in pages("assets.json"):
        name, asset_id = asset.get("name"), asset.get("id")
        if not managed(name):
            fail("Draft contains an asset not managed by this publishing script")
        if name in remote:
            fail(f"Duplicate remote asset: {name}")
        if type(asset_id) is not int or asset_id <= 0:
            fail(f"{name} has an invalid asset identifier")
        remote[name] = asset
    if command == "inventory":
        write_text("stale.txt", "".join(
            str(remote[name]["id"]) + "\n" for name in sorted(remote.keys() - local.keys())
        ))
    elif command == "replace":
        name = Path(sys.argv[3]).name
        if name not in local:
            fail("Upload path is not a local release artifact")
        if name in remote:
            print(remote[name]["id"])
    else:
        if remote.keys() != local.keys():
            fail("Remote asset names do not match the local artifact set")
        for name, expected in local.items():
            asset = remote[name]
            if asset.get("state") != "uploaded":
                fail(f"{name} has not finished uploading")
            if asset.get("size") != expected["size"]:
                fail(f"{name} has an unexpected remote size")
            if asset.get("digest") != expected["digest"]:
                fail(f"{name} has a missing or mismatched remote SHA-256 digest")
        print(f"Verified {len(local)} release assets against GitHub SHA-256 digests.")
elif command == "upload-url":
    print((state / "upload-base.txt").read_text(encoding="utf-8") +
          "?" + urlencode({"name": Path(sys.argv[3]).name}))
elif command == "published":
    release = read_json("published.json")
    identity(release, sys.argv[3], False)
    url = release.get("html_url", "")
    if not url.startswith(f'https://github.com/{os.environ["GH_REPO"]}/releases/'):
        fail("Unexpected published release URL")
    print(url)
else:
    fail("Unknown validation command")
PY

validate() {
  python3 "$state_dir/validate.py" "$state_dir" "$@"
}

validate local "$dist"

# Resolve annotated tags too, and reject a release tag for a different build.
gh api "repos/$GH_REPO/git/ref/tags/$RELEASE_VERSION" > "$state_dir/tag.json"
tag_resolved=false
for _ in {1..10}; do
  read -r object_type object_sha < <(validate tag)
  if [[ "$object_type" == commit ]]; then
    [[ "$object_sha" == "$source_commit" ]] || fail "Remote tag differs from SOURCE_COMMIT_SHA"
    tag_resolved=true
    break
  fi
  gh api "repos/$GH_REPO/git/tags/$object_sha" > "$state_dir/tag.json"
done
[[ "$tag_resolved" == true ]] || fail "Remote tag chain is too deep"

# A draft can be absent from the list immediately after POST. Capture its ID
# from the creation response and use that ID for every subsequent operation.
gh api --paginate --slurp "repos/$GH_REPO/releases?per_page=100" > "$state_dir/releases.json"
release_id="$(validate release)"
if [[ "$release_id" == missing ]]; then
  validate payload create "$notes_file" "$source_commit"
  gh api --method POST "repos/$GH_REPO/releases" --input "$state_dir/payload.json" \
    > "$state_dir/release.json"
  release_id="$(validate draft "")"
fi

get_draft() {
  local read_attempt
  for read_attempt in 1 2 3; do
    if gh api "repos/$GH_REPO/releases/$release_id" > "$state_dir/release.json"; then
      validate draft "$release_id" > /dev/null
      return
    fi
    [[ "$read_attempt" -eq 3 ]] || sleep "$((read_attempt * 5))"
  done
  fail "Draft release could not be read by its identifier"
}

get_assets() {
  gh api --paginate --slurp "repos/$GH_REPO/releases/$release_id/assets?per_page=100" \
    > "$state_dir/assets.json"
}

get_draft
get_assets
validate inventory
while IFS= read -r stale_id; do
  gh api --method DELETE "repos/$GH_REPO/releases/assets/$stale_id"
done < "$state_dir/stale.txt"

# Always include tag_name in PATCH; GitHub can otherwise clear a draft's tag.
get_draft
validate payload metadata "$notes_file"
gh api --method PATCH "repos/$GH_REPO/releases/$release_id" --input "$state_dir/payload.json" \
  > "$state_dir/updated.json"

mapfile -d '' -t assets < "$state_dir/files.nul"
for asset in "${assets[@]}"; do
  uploaded=false
  for attempt in 1 2 3; do
    get_draft
    get_assets
    # Re-read by ID on each attempt: a failed client may have stored an asset,
    # including a starter asset, before losing the successful upload response.
    replace_id="$(validate replace "$asset")"
    if [[ -n "$replace_id" ]]; then
      gh api --method DELETE "repos/$GH_REPO/releases/assets/$replace_id"
    fi
    upload_url="$(validate upload-url "$asset")"
    # gh streams --input files and sends their stat size as Content-Length.
    if gh api --method POST "$upload_url" --header 'Content-Type: application/octet-stream' \
      --input "$asset" > "$state_dir/upload.json"; then
      uploaded=true
      break
    fi
    if [[ "$attempt" -lt 3 ]]; then
      printf 'Retrying %s after upload attempt %s.\n' "${asset##*/}" "$attempt" >&2
      sleep "$((attempt * 5))"
    fi
  done
  [[ "$uploaded" == true ]] || fail "Upload failed after three attempts: ${asset##*/}"
done

get_assets
validate verify
get_draft
validate payload publish
gh api --method PATCH "repos/$GH_REPO/releases/$release_id" --input "$state_dir/payload.json" \
  > "$state_dir/updated.json"
gh api "repos/$GH_REPO/releases/$release_id" > "$state_dir/published.json"
release_url="$(validate published "$release_id")"
printf 'Release published: %s\n' "$release_url"
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  printf '\nRelease: [%s](%s)\n' "$RELEASE_VERSION" "$release_url" >> "$GITHUB_STEP_SUMMARY"
fi
