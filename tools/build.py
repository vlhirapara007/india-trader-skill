#!/usr/bin/env python3
"""Sync core/indtrader.py into every india-* skill folder, then zip each skill into dist/.

  python3 tools/build.py          sync + zip
  python3 tools/build.py --check  fail if any copy is stale (for CI)
"""

import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(ROOT, "core", "indtrader.py")


def skills():
    return sorted(d for d in os.listdir(ROOT)
                  if d.startswith("india-") and os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))


def main():
    check = "--check" in sys.argv
    with open(CORE, "rb") as fh:
        core = fh.read()
    stale = []
    for s in skills():
        dst = os.path.join(ROOT, s, "indtrader.py")
        cur = open(dst, "rb").read() if os.path.exists(dst) else None
        if cur != core:
            stale.append(s)
            if not check:
                shutil.copyfile(CORE, dst)
    if check:
        if stale:
            sys.exit("stale core copy in: " + ", ".join(stale))
        print("all %d skills carry the current core" % len(skills()))
        return
    dist = os.path.join(ROOT, "dist")
    os.makedirs(dist, exist_ok=True)
    for s in skills():
        zp = os.path.join(dist, s + ".zip")
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for base, dirs, files in os.walk(os.path.join(ROOT, s)):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for f in sorted(files):
                    full = os.path.join(base, f)
                    z.write(full, os.path.relpath(full, ROOT))
        print("built dist/%s.zip%s" % (s, "  (core synced)" if s in stale else ""))


if __name__ == "__main__":
    main()
