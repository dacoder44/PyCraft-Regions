"""Download checksum-pinned upstream integration fixtures to an ignored cache.

Requires network access to api.github.com. Never downloads into a world folder.
Sources/license: PrismarineJS/prismarine-provider-anvil (MIT), see manifest.
"""

import base64
import hashlib
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    manifest = json.loads((ROOT / "tests/real_fixtures.json").read_text())
    for fixture in manifest["files"]:
        relative = Path(fixture["path"]).relative_to("test/fixtures")
        output = ROOT / ".cache/real" / relative
        if output.exists() and hashlib.sha256(output.read_bytes()).hexdigest() == fixture["sha256"]:
            print("Cached:", relative)
            continue
        url = f"https://api.github.com/repos/{manifest['repository']}/git/blobs/{fixture['blob']}"
        with urllib.request.urlopen(url, timeout=60) as response:
            data = base64.b64decode(json.load(response)["content"])
        if hashlib.sha256(data).hexdigest() != fixture["sha256"]:
            raise RuntimeError(f"Fixture checksum mismatch: {relative}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        print("Downloaded:", relative)


if __name__ == "__main__":
    main()
