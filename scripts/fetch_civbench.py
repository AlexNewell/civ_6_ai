"""Download the public CivBench dataset into the gitignored datasets/ folder.

The revision is pinned, and every file is checked against the hash Hugging Face lists for it, so
repeat runs give the same bytes and skip what's already there. Only the curated tables are
fetched, not the 7 GB of Inspect logs or the raw streams they're built from. CivBench is CC BY 4.0:
see THIRD_PARTY_NOTICES.md for the credit.
"""

import argparse
import concurrent.futures
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPO = "civbench/civbench-v1"
REVISION = "2d9937d1e273a18fbe0e9e8f32325d4d6cb61dea"
DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "datasets" / "civbench"
USER_AGENT = "civ6-ai-dataset-fetch"
TREE_URL = f"https://huggingface.co/api/datasets/{REPO}/tree/{REVISION}?recursive=true&expand=true"
FILE_URL = f"https://huggingface.co/datasets/{REPO}/resolve/{REVISION}/"

# This allowlist also stops a bad listing from writing outside the target folder.
WANTED_PATH = re.compile(r"LICENSE|tables/[a-z_]+\.parquet")
NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')
CHUNK_BYTES = 1024 * 1024
ATTEMPTS = 3
TIMEOUT_SECONDS = 120
WORKERS = 6


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int
    algorithm: str  # "sha256" for LFS files, "git-sha1" for files stored directly in git
    digest: str


def parse_entry(entry: dict) -> RemoteFile | None:
    """Turn one listing entry into a file to fetch, or None if it isn't wanted."""
    if entry.get("type") != "file" or not WANTED_PATH.fullmatch(entry["path"]):
        return None
    lfs = entry.get("lfs")
    if lfs:
        return RemoteFile(entry["path"], lfs["size"], "sha256", lfs["oid"])
    return RemoteFile(entry["path"], entry["size"], "git-sha1", entry["oid"])


def next_page(link_header: str | None) -> str | None:
    match = NEXT_LINK.search(link_header or "")
    return match.group(1) if match else None


def request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


def list_files(url: str = TREE_URL) -> Iterator[RemoteFile]:
    """Yield every wanted file, following the listing's pages."""
    next_url: str | None = url
    while next_url:
        with urllib.request.urlopen(request(next_url), timeout=TIMEOUT_SECONDS) as response:
            entries = json.load(response)
            next_url = next_page(response.headers.get("Link"))
        for entry in entries:
            if (remote := parse_entry(entry)) is not None:
                yield remote


def file_digest(path: Path, algorithm: str, size: int) -> str:
    hasher = hashlib.sha256() if algorithm == "sha256" else hashlib.sha1()
    if algorithm == "git-sha1":
        hasher.update(f"blob {size}\0".encode())
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            hasher.update(chunk)
    return hasher.hexdigest()


def is_valid(path: Path, remote: RemoteFile) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == remote.size
        and file_digest(path, remote.algorithm, remote.size) == remote.digest
    )


def download(remote: RemoteFile, root: Path) -> bool:
    """Fetch one file unless a valid copy exists. Returns True if it was downloaded."""
    target = root / remote.path
    if is_valid(target, remote):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with (
                urllib.request.urlopen(
                    request(FILE_URL + remote.path), timeout=TIMEOUT_SECONDS
                ) as response,
                partial.open("wb") as handle,
            ):
                while chunk := response.read(CHUNK_BYTES):
                    handle.write(chunk)
            if is_valid(partial, remote):
                partial.replace(target)
                return True
            problem = "hash or size mismatch"
        except (OSError, urllib.error.URLError) as error:
            problem = str(error)
        print(f"Attempt {attempt} failed for {remote.path}: {problem}", file=sys.stderr)
        if attempt < ATTEMPTS:
            time.sleep(2**attempt)
    partial.unlink(missing_ok=True)
    raise RuntimeError(f"Could not fetch {remote.path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Download folder")
    args = parser.parse_args()

    root = args.root / REVISION[:12]
    remotes = list(list_files())
    total_mb = sum(remote.size for remote in remotes) / 1_000_000
    print(f"{len(remotes)} files, {total_mb:.0f} MB, revision {REVISION[:12]}, into {root}")
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        fetched = sum(pool.map(lambda remote: download(remote, root), remotes))
    print(f"Done: {fetched} downloaded, {len(remotes) - fetched} already valid")


if __name__ == "__main__":
    main()
