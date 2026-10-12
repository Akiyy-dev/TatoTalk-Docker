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
from pathlib import Path
import re
import sys

state = Path(sys.argv[1])
command = sys.argv[2]
required = {"TatoTalk-Docker.zip", "image-manifest.json", "SHA256SUMS.txt"}
part_pattern = re.compile(r"tatotalk-android-amd64\.tar\.gz\.part-[0-9]{4}")

def fail(message):
    raise SystemExit("Release validation failed: " + message)

def managed(name):
    return isinstance(name, str) and (name in required or part_pattern.fullmatch(name))

def pages(filename):
    data = json.loads((state / filename).read_text(encoding="utf-8"))
    if not isinstance(data, list) or any(not isinstance(page, list) for page in data):
        fail("Invalid paginated GitHub response")
    return [entry for page in data for entry in page]

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
    (state / "local.json").write_text(json.dumps(manifest), encoding="utf-8")
    (state / "files.nul").write_bytes(
        b"".join(path.as_posix().encode("utf-8") + b"\0" for path in files)
    )
elif command == "tag":
    response = json.loads((state / "tag.json").read_text(encoding="utf-8"))
    obj = response.get("object", {})
    kind, sha = obj.get("type"), obj.get("sha", "")
    if kind not in {"commit", "tag"} or not re.fullmatch(r"[a-fA-F0-9]{40}", sha):
        fail("Remote tag does not resolve to a commit")
    print(kind, sha.lower())
elif command == "release":
    matches = [entry for entry in pages("releases.json") if entry.get("tag_name") == sys.argv[3]]
    if not matches:
        print("missing")
    else:
        if len(matches) != 1 or matches[0].get("draft") is not True:
            fail("An already published release cannot be overwritten")
        release_id = matches[0].get("id")
        if not isinstance(release_id, int) or release_id <= 0:
            fail("Invalid release identifier")
        print(release_id)
elif command in {"inventory", "verify"}:
    local = json.loads((state / "local.json").read_text(encoding="utf-8"))
    remote = {}
    for asset in pages("assets.json"):
        name = asset.get("name")
        if not managed(name):
            fail("Draft contains an asset not managed by this publishing script")
        if name in remote:
            fail(f"Duplicate remote asset: {name}")
        remote[name] = asset
    if command == "inventory":
        (state / "stale.txt").write_text(
            "".join(name + "\n" for name in sorted(remote.keys() - local.keys())),
            encoding="utf-8",
        )
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
elif command == "published":
    release = json.loads((state / "published.json").read_text(encoding="utf-8"))
    url = release.get("html_url", "")
    if release.get("draft") is not False or not url.startswith("https://github.com/"):
        fail("GitHub has not confirmed publication")
    print(url)
else:
    fail("Unknown validation command")
PY

python3 "$state_dir/validate.py" "$state_dir" local "$dist"

# Resolve annotated tags too, and reject a release tag for a different build.
gh api "repos/$GH_REPO/git/ref/tags/$RELEASE_VERSION" > "$state_dir/tag.json"
tag_resolved=false
for _ in {1..10}; do
  read -r object_type object_sha < <(python3 "$state_dir/validate.py" "$state_dir" tag)
  if [[ "$object_type" == commit ]]; then
    [[ "$object_sha" == "$source_commit" ]] || fail "Remote tag differs from SOURCE_COMMIT_SHA"
    tag_resolved=true
    break
  fi
  gh api "repos/$GH_REPO/git/tags/$object_sha" > "$state_dir/tag.json"
done
[[ "$tag_resolved" == true ]] || fail "Remote tag chain is too deep"

find_draft() {
  gh api --paginate --slurp "repos/$GH_REPO/releases?per_page=100" > "$state_dir/releases.json"
  python3 "$state_dir/validate.py" "$state_dir" release "$RELEASE_VERSION"
}

release_id="$(find_draft)"
if [[ "$release_id" == missing ]]; then
  gh release create "$RELEASE_VERSION" --repo "$GH_REPO" --verify-tag --draft \
    --target "$source_commit" --title "TatoTalk Docker $RELEASE_VERSION" --notes-file "$notes_file"
  release_id="$(find_draft)"
  [[ "$release_id" != missing ]] || fail "Created draft could not be found"
fi

gh api --paginate --slurp "repos/$GH_REPO/releases/$release_id/assets?per_page=100" \
  > "$state_dir/assets.json"
python3 "$state_dir/validate.py" "$state_dir" inventory
while IFS= read -r stale_asset; do
  gh release delete-asset "$RELEASE_VERSION" "$stale_asset" --repo "$GH_REPO" --yes
done < "$state_dir/stale.txt"
gh release edit "$RELEASE_VERSION" --repo "$GH_REPO" \
  --title "TatoTalk Docker $RELEASE_VERSION" --notes-file "$notes_file"

mapfile -d '' -t assets < "$state_dir/files.nul"
for asset in "${assets[@]}"; do
  uploaded=false
  for attempt in 1 2 3; do
    if gh release upload "$RELEASE_VERSION" "$asset" --repo "$GH_REPO" --clobber; then
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

gh api --paginate --slurp "repos/$GH_REPO/releases/$release_id/assets?per_page=100" \
  > "$state_dir/assets.json"
python3 "$state_dir/validate.py" "$state_dir" verify
[[ "$(find_draft)" == "$release_id" ]] || fail "Draft release changed during upload"
gh release edit "$RELEASE_VERSION" --repo "$GH_REPO" --draft=false
gh api "repos/$GH_REPO/releases/$release_id" > "$state_dir/published.json"
release_url="$(python3 "$state_dir/validate.py" "$state_dir" published)"
printf 'Release published: %s\n' "$release_url"
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  printf '\nRelease: [%s](%s)\n' "$RELEASE_VERSION" "$release_url" >> "$GITHUB_STEP_SUMMARY"
fi
