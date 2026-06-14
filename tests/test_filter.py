"""
Smoke tests for the keyword filter.

Run from project root: python -m pytest tests/ -v
Or directly: python tests/test_filter.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scanrelay.config import FilterConfig
from scanrelay.keyword_filter import Filter


def run_cases(cases: list[tuple[str, bool, str]], label: str) -> int:
    """Each case: (transcript, should_hit, description). Returns failure count."""
    cfg = FilterConfig()
    f = Filter(cfg)
    fails = 0
    print(f"\n=== {label} ===")
    for text, expected, desc in cases:
        hit = f.find_hit(text)
        got = hit is not None
        ok = got == expected
        status = "PASS" if ok else "FAIL"
        match_info = f" -> matched '{hit.matched_text}'" if hit else ""
        print(f"  [{status}] {desc}: {text!r}{match_info}")
        if not ok:
            fails += 1
    return fails


def main() -> int:
    fails = 0

    fails += run_cases([
        ("Engine 7 responding to My Keyword for a grass fire", True, "plain my keyword"),
        ("structure fire at My Kw Rd and CR 201", True, "my kw abbrev"),
        ("dispatch to MYKEYWORD marina", True, "mykeyword one word, caps"),
        ("traffic stop on My-Keyword Drive", True, "hyphenated"),
        ("nothing relevant here", False, "no match"),
        ("welfare check at Cross Lake estates", False, "looks similar but different"),
    ], "My Keyword keywords")

    fails += run_cases([
        ("MVA at 12345 Lake Road", True, "12345 digits"),
        ("respond to CR 12345", True, "county road 12345"),
        ("FM 12345 north of town", True, "farm road 12345"),
        ("grass fire near twelve oh one Hickory", True, "twelve oh one word form"),
        ("address is twelve-oh-one Main Street", True, "hyphenated twelve-oh-one"),
        ("at twelve hundred and one Elm", True, "twelve hundred and one"),
        ("at one thousand two hundred one Elm", True, "one thousand two hundred one"),
        # Should NOT match:
        ("dispatched to 123450 Highway 82", False, "12345 prefix of longer number"),
        ("call from 312345 system", False, "12345 suffix of longer number"),
        ("MVA at 12340 Lake Road", False, "12340 not 12345"),
        ("MVA at 12346 Lake Road", False, "12346 not 12345"),
        ("twelve oh two Main", False, "twelve oh two"),
        ("at twelve hundred two", False, "twelve hundred two"),
    ], "12345 patterns")

    print(f"\n{'-' * 40}")
    print(f"Failures: {fails}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
