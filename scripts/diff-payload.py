#!/usr/bin/env python3
"""Compares a collector's payload with the expected one, allowing only the
collector implementation name to differ. Exit 1 with the differences."""
import json
import sys

got = json.load(open(sys.argv[1]))
expected = json.load(open(sys.argv[2]))
expected["collector"]["implementation"] = sys.argv[3]
problems = []


def diff(a, b, path):
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            diff(a.get(key, "<missing>"), b.get(key, "<missing>"), path + "." + key)
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for index, (x, y) in enumerate(zip(a, b)):
            diff(x, y, "%s[%d]" % (path, index))
    elif a != b:
        problems.append("%s: got %r, expected %r" % (path, a, b))


diff(got, expected, "payload")
for problem in problems:
    print(problem)
print("%s: %s" % (sys.argv[3], "matches the expected payload" if not problems else "%d difference(s)" % len(problems)))
sys.exit(1 if problems else 0)
