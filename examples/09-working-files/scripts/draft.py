"""Render a profile's facts into a markdown bio (stdlib only, deterministic).

facts.txt format: line 1 = name, line 2 = role, remaining non-empty lines = highlights.
"""

import sys
from pathlib import Path


def main() -> None:
    facts, out = sys.argv[1], Path(sys.argv[2])
    lines = [ln for ln in Path(facts).read_text().splitlines() if ln.strip()]
    name, role, *highlights = lines
    body = [f"# {name}", "", f"**{role}**", ""] + [f"- {h}" for h in highlights]
    out.write_text("\n".join(body) + "\n")


if __name__ == "__main__":
    main()
