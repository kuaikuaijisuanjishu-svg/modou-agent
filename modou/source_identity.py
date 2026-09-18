"""Working source identity, independent of staged versus untracked status."""
import hashlib
import subprocess
from pathlib import Path


def source_content_sha256(root: Path) -> str:
    names = subprocess.run(["git", "ls-files", "-z", "--cached", "--others",
                            "--exclude-standard"], cwd=root, check=True,
                           capture_output=True, timeout=20).stdout.split(b"\0")
    h = hashlib.sha256()
    for name in sorted(set(names) - {b""}):
        p = root / name.decode("utf-8", "surrogateescape")
        if not p.exists() and not p.is_symlink():
            continue
        h.update(len(name).to_bytes(8, "big") + name)
        if p.is_symlink():
            data = b"symlink:" + str(p.readlink()).encode("utf-8", "surrogateescape")
        elif p.is_file():
            data = b"file:" + str(p.stat().st_mode & 0o777).encode() + b":" + p.read_bytes()
        else:
            data = b"directory"
        h.update(hashlib.sha256(data).digest())
    return h.hexdigest()
