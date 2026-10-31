"""Build the pinned official SQLite library in a disposable compiler stage."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

URL = "https://www.sqlite.org/2026/sqlite-autoconf-3540100.tar.gz"
# Published on the official SQLite download page; checked independently.
SHA3_256 = "b6ced585c750916b1052667239a12c25a7ff34e7f06e1e6e01ecb0427657d9c4"

with tempfile.TemporaryDirectory(prefix="sqlite-build-") as work:
    folder = Path(work)
    archive = Path(sys.argv[1]) if len(sys.argv) == 2 else folder / "source.tar.gz"
    if len(sys.argv) != 2:
        with urllib.request.urlopen(URL, timeout=60) as source, archive.open("wb") as sink:
            shutil.copyfileobj(source, sink)
    if hashlib.sha3_256(archive.read_bytes()).hexdigest() != SHA3_256:
        raise RuntimeError("Official SQLite SHA3-256 verification failed")
    with tarfile.open(archive, "r:gz") as source:
        members = [m for m in source if m.name.endswith(("/sqlite3.c", "/sqlite3.h", "/sqlite3ext.h"))]
        for member in members:
            if not member.isfile() or ".." in Path(member.name).parts or member.name.startswith("/"):
                raise RuntimeError("Unexpected SQLite source archive member")
            with source.extractfile(member) as content, (folder / Path(member.name).name).open("wb") as sink:
                shutil.copyfileobj(content, sink)
    library = Path("/opt/sqlite/lib")
    library.mkdir(parents=True)
    subprocess.run(["gcc", "-O2", "-fPIC", "-shared", "-DSQLITE_THREADSAFE=1", "-DSQLITE_ENABLE_FTS5",
                    "-DSQLITE_ENABLE_RTREE", "-DSQLITE_ENABLE_COLUMN_METADATA", "-DSQLITE_ENABLE_DBSTAT_VTAB",
                    "-DSQLITE_DQS=0", "-Wl,-soname,libsqlite3.so.0", str(folder / "sqlite3.c"),
                    "-lpthread", "-ldl", "-lm", "-o", str(library / "libsqlite3.so.0")], check=True)
    (library / "libsqlite3.so").symlink_to("libsqlite3.so.0")
    (library / "SOURCE.txt").write_text(f"{URL}\nSHA3-256 {SHA3_256}\n")
