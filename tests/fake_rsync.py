#!/usr/bin/env python3
"""Minimal stand-in for rsync, for tests on machines without it.

Supports local destinations only, and just the flags archiver uses:
copy mode prints --info=progress2-style lines (CR-separated, like rsync);
--checksum --dry-run --itemize-changes prints '>fc...' for differing files.

Env knobs: FAKE_RSYNC_FAIL=1 exits 23; FAKE_RSYNC_SLEEP=<sec> sleeps per chunk.
"""
import hashlib
import os
import shutil
import sys
import time


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv):
    flags = [a for a in argv if a.startswith("-")]
    paths = [a for a in argv if not a.startswith("-")]
    *sources, dest = paths
    if os.environ.get("FAKE_RSYNC_FAIL"):
        print("rsync: [sender] simulated failure", file=sys.stderr)
        return 23
    os.makedirs(dest, exist_ok=True)
    delay = float(os.environ.get("FAKE_RSYNC_SLEEP", "0"))

    if "--dry-run" in flags:
        for src in sources:
            target = os.path.join(dest, os.path.basename(src))
            if not os.path.exists(target):
                print(f">f+++++++++ {os.path.basename(src)}")
            elif md5(src) != md5(target):
                print(f">fc.t...... {os.path.basename(src)}")
        return 0

    total = sum(os.path.getsize(s) for s in sources) or 1
    done = 0
    for src in sources:
        target = os.path.join(dest, os.path.basename(src))
        with open(src, "rb") as fin, open(target + ".tmp", "wb") as fout:
            for chunk in iter(lambda: fin.read(65536), b""):
                fout.write(chunk)
                done += len(chunk)
                pct = done * 100 // total
                sys.stdout.write(f"\r{done:>15,} {pct:>3}%    1.00MB/s    0:00:00")
                sys.stdout.flush()
                if delay:
                    time.sleep(delay)
        shutil.copystat(src, target + ".tmp")
        os.replace(target + ".tmp", target)
    sys.stdout.write(f"\r{done:>15,} 100%    1.00MB/s    0:00:00 (xfr#{len(sources)}, to-chk=0/{len(sources)})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
