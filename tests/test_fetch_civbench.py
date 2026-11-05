import hashlib
import io
from pathlib import Path

import pytest

import fetch_civbench
from fetch_civbench import RemoteFile, download, file_digest, next_page, parse_entry


def git_entry(path: str, size: int = 5, oid: str = "abc") -> dict:
    return {"type": "file", "path": path, "size": size, "oid": oid}


def test_parse_entry_uses_git_hash_for_plain_files():
    assert parse_entry(git_entry("LICENSE")) == RemoteFile("LICENSE", 5, "git-sha1", "abc")


def test_parse_entry_prefers_lfs_hash():
    entry = git_entry("tables/games.parquet") | {"lfs": {"oid": "def", "size": 9}}
    assert parse_entry(entry) == RemoteFile("tables/games.parquet", 9, "sha256", "def")


@pytest.mark.parametrize(
    "path",
    ["inspect_logs/a.eval", "raw/runs/a/diary.jsonl", "tables/../../evil.parquet", "README.md"],
)
def test_parse_entry_skips_unwanted_and_unsafe_paths(path):
    assert parse_entry(git_entry(path)) is None


def test_parse_entry_skips_directories():
    assert parse_entry({"type": "directory", "path": "tables"}) is None


def test_next_page_reads_link_header():
    assert next_page('<https://example.test/next>; rel="next"') == "https://example.test/next"
    assert next_page(None) is None
    assert next_page('<https://example.test/prev>; rel="prev"') is None


def test_git_digest_matches_git(tmp_path: Path):
    path = tmp_path / "hello"
    path.write_bytes(b"hello\n")
    expected = "ce013625030ba8dba906f756967f9e9ca394464a"  # git hash-object of this content
    assert file_digest(path, "git-sha1", 6) == expected


def lfs_file(content: bytes) -> RemoteFile:
    digest = hashlib.sha256(content).hexdigest()
    return RemoteFile("tables/games.parquet", len(content), "sha256", digest)


def serve(monkeypatch: pytest.MonkeyPatch, bodies: list[bytes]) -> None:
    queue = list(bodies)
    monkeypatch.setattr(
        fetch_civbench.urllib.request, "urlopen", lambda *_, **__: io.BytesIO(queue.pop(0))
    )
    monkeypatch.setattr(fetch_civbench.time, "sleep", lambda _: None)


def test_download_writes_verified_file_then_skips_it(tmp_path, monkeypatch):
    remote = lfs_file(b"table")
    serve(monkeypatch, [b"table"])
    assert download(remote, tmp_path) is True
    assert (tmp_path / remote.path).read_bytes() == b"table"
    assert download(remote, tmp_path) is False


def test_download_retries_a_corrupt_file(tmp_path, monkeypatch):
    remote = lfs_file(b"table")
    serve(monkeypatch, [b"tabl!", b"table"])
    assert download(remote, tmp_path) is True
    assert (tmp_path / remote.path).read_bytes() == b"table"


def test_download_never_keeps_a_file_that_fails_verification(tmp_path, monkeypatch):
    remote = lfs_file(b"table")
    serve(monkeypatch, [b"tabl!"] * 3)
    with pytest.raises(RuntimeError):
        download(remote, tmp_path)
    assert list((tmp_path / "tables").iterdir()) == []
