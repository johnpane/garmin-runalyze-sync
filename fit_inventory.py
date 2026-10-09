#!/usr/bin/env python3
"""Compare two FIT files: which messages and which fields each one contains.

Used to determine whether Garmin's API-served FIT file has had messages or
fields stripped relative to a manual "Export Original" download.

Pass the KNOWN-COMPLETE file first (the manual export) and the suspect file
second. The diff reports what the second file is missing relative to the first.

Undocumented Garmin messages appear as unknown_<globalNum>.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

import fitdecode


def field_label(obj) -> str:
    """Label a fitdecode field definition or field value as 'name#defNum'.

    Attribute names differ between fitdecode's definitions and values, so this
    probes for them instead of assuming one shape.
    """
    num = getattr(obj, "field_def_num", None)
    if num is None:
        num = getattr(obj, "def_num", None)

    inner = getattr(obj, "field", None)
    name = getattr(inner, "name", None) if inner is not None else None
    if name is None:
        name = getattr(obj, "name", None)
    if name is None:
        name = "unknown"

    return f"{name}#{num}" if num is not None else str(name)


def scan(path: Path):
    """Return (data_message_counts, field_label_sets, names, def_counts, header)."""
    counts: Counter[int] = Counter()
    fields: dict[int, set[str]] = defaultdict(set)
    names: dict[int, str] = {}
    def_counts: Counter[int] = Counter()
    header = None

    reader = fitdecode.FitReader(
        str(path),
        check_crc=fitdecode.CrcCheck.DISABLED,
        error_handling=fitdecode.ErrorHandling.IGNORE,
    )
    with reader as fit:
        for frame in fit:
            if frame.frame_type == fitdecode.FIT_FRAME_HEADER:
                if header is None:
                    header = (
                        f"proto={frame.proto_ver} profile={frame.profile_ver} "
                        f"body={frame.body_size}"
                    )
            elif frame.frame_type == fitdecode.FIT_FRAME_DEFINITION:
                gnum = frame.global_mesg_num
                names.setdefault(gnum, frame.name)
                def_counts[gnum] += 1
                for fdef in list(frame.field_defs) + list(frame.dev_field_defs or ()):
                    fields[gnum].add(field_label(fdef))
            elif frame.frame_type == fitdecode.FIT_FRAME_DATA:
                gnum = frame.global_mesg_num
                names.setdefault(gnum, frame.name)
                counts[gnum] += 1
                for field in frame.fields:
                    fields[gnum].add(field_label(field))

    return counts, fields, names, def_counts, header


def describe(label: str, path: Path) -> tuple:
    if not path.exists():
        raise SystemExit(f"{label}: no such file: {path}")
    result = scan(path)
    counts, fields, names, def_counts, header = result

    total_records = sum(counts.values())
    print(f"== {label}: {path}")
    print(f"   {header or 'no header frame'}")
    print(f"   {len(counts)} message types, {total_records} data messages, "
          f"{path.stat().st_size} bytes")

    for gnum in sorted(set(counts) | set(fields)):
        name = names.get(gnum, "?")
        field_list = ", ".join(sorted(fields.get(gnum, ())))
        print(f"   msg {gnum:>4}  {name:<24} n={counts.get(gnum, 0):>6}  "
              f"defs={def_counts.get(gnum, 0):>3}  fields=[{field_list}]")

    unknowns = sorted(g for g, n in names.items() if n.startswith("unknown_"))
    print(f"   undocumented messages: "
          f"{unknowns if unknowns else 'none'}")
    print()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path,
                        help="file known to be complete (manual Export Original)")
    parser.add_argument("candidate", type=Path,
                        help="file under test (API-served / script-downloaded)")
    args = parser.parse_args(argv)

    a_counts, a_fields, a_names, a_defs, _ = describe("BASELINE (complete)", args.baseline)
    b_counts, b_fields, b_names, b_defs, _ = describe("CANDIDATE (under test)", args.candidate)

    a_keys = set(a_counts) | set(a_fields)
    b_keys = set(b_counts) | set(b_fields)

    only_a = sorted(a_keys - b_keys)
    only_b = sorted(b_keys - a_keys)
    common = sorted(a_keys & b_keys)

    missing_fields: dict[int, list[str]] = {}
    for gnum in common:
        gone = a_fields.get(gnum, set()) - b_fields.get(gnum, set())
        if gone:
            missing_fields[gnum] = sorted(gone)

    extra_fields: dict[int, list[str]] = {}
    for gnum in common:
        added = b_fields.get(gnum, set()) - a_fields.get(gnum, set())
        if added:
            extra_fields[gnum] = sorted(added)

    count_deltas = [
        (gnum, a_counts.get(gnum, 0), b_counts.get(gnum, 0))
        for gnum in common
        if a_counts.get(gnum, 0) != b_counts.get(gnum, 0)
    ]

    def label(gnum: int) -> str:
        return f"{gnum} ({a_names.get(gnum) or b_names.get(gnum) or '?'})"

    print("=== DIFF  baseline -> candidate")
    print(f"   message types only in baseline : "
          f"{[label(g) for g in only_a] or 'none'}")
    print(f"   message types only in candidate: "
          f"{[label(g) for g in only_b] or 'none'}")

    if count_deltas:
        print("   data message count changes:")
        for gnum, before, after in count_deltas:
            print(f"     msg {label(gnum)}: {before} -> {after}")

    if missing_fields:
        print("   FIELDS MISSING from candidate:")
        for gnum, gone in missing_fields.items():
            print(f"     msg {label(gnum)}: {gone}")
    else:
        print("   fields missing from candidate: none")

    if extra_fields:
        print("   fields present only in candidate:")
        for gnum, added in extra_fields.items():
            print(f"     msg {label(gnum)}: {added}")

    stripped_unknowns = [label(g) for g in only_a if a_names.get(g, "").startswith("unknown_")]
    if stripped_unknowns:
        print(f"   undocumented messages removed by Garmin: {stripped_unknowns}")

    complete = not only_a and not missing_fields and not count_deltas
    print()
    if complete:
        print("VERDICT: candidate contains every message type, field and record "
              "count the baseline has. The download path is a full-data source.")
    else:
        print("VERDICT: candidate is NOT equivalent to the baseline. Garmin is "
              "serving a reduced file on this path; only the baseline-style "
              "sources (manual export, GDPR export, pre-upload device file) "
              "carry the missing data.")
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
