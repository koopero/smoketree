"""Author reconcile (Phase 2): surface and resolve template drift in authored copies.

An authored output keeps three things: the generated ``name.template.ext``, the
human-owned ``name.ext``, and a stored **fork-base** (the template content captured when
the copy was first seeded). When the generator moves the template away from the fork-base,
the copy has *drifted* — there are upstream changes the human hasn't seen.

``find_drift`` reports drifted authored files; ``resolve`` acts on one:

- ``merge``          — 3-way merge the template's changes into the copy (text only),
- ``take-generated`` — replace the copy with the current template,
- ``keep-mine``      — leave the copy, just dismiss the drift.

Each action advances the fork-base to the current template, so the drift clears until the
generator moves again.
"""

from __future__ import annotations

import glob as globlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .bind import Pattern, template_pattern
from .errors import SmoketreeError
from .project import Project
from .rules import LoadedPipeline


@dataclass
class Drift:
    """One authored copy whose generated template has moved since the fork-base."""

    rule: str
    keys: dict[str, str]
    authored: Path     # the human-owned copy
    template: Path     # the current generated template
    base: Path         # the stored fork-base (may not exist for a pre-fork copy)

    @property
    def is_text(self) -> bool:
        for path in (self.template, self.authored):
            try:
                path.read_text()
            except (UnicodeDecodeError, OSError):
                return False
        return True

    @property
    def copy_edited(self) -> bool:
        """Whether the human edited the copy away from the fork-base."""
        base = self.base.read_bytes() if self.base.exists() else b""
        return self.authored.read_bytes() != base

    @property
    def label(self) -> str:
        keys = " · ".join(self.keys[k] for k in sorted(self.keys))
        return f"{self.rule}: {keys}" if keys else self.rule


def find_drift(project: Project, loaded: LoadedPipeline) -> list[Drift]:
    """Every authored copy whose template content differs from its stored fork-base."""
    root = project.root
    base_root = project.forkbase_root / loaded.id
    drifts: list[Drift] = []
    for rule in loaded.rules:
        if not rule.enabled or not rule.author:
            continue
        for port in rule.author:
            decl_pat = Pattern.compile(rule.out[port])
            tpl_pat = Pattern.compile(template_pattern(rule.out[port]))
            for rel in globlib.glob(tpl_pat.glob_str, root_dir=str(root), recursive=True):
                rel = rel.replace("\\", "/")
                m = tpl_pat.regex.match(rel)
                if not m:
                    continue
                template = root / rel
                if not template.is_file():
                    continue
                authored_rel = decl_pat.fill(m.groupdict())
                authored = root / authored_rel
                if not authored.exists():
                    continue  # not seeded yet — nothing to reconcile
                base = base_root / authored_rel
                base_bytes = base.read_bytes() if base.exists() else b""
                if template.read_bytes() == base_bytes:
                    continue  # generator hasn't moved since the fork
                drifts.append(
                    Drift(rule=rule.name, keys=m.groupdict(), authored=authored,
                          template=template, base=base)
                )
    return drifts


def resolve(drift: Drift, action: str) -> str:
    """Apply ``action`` (``merge`` | ``take-generated`` | ``keep-mine``) to one drift."""
    if action == "take-generated":
        shutil.copyfile(drift.template, drift.authored)
        status = "took generated"
    elif action == "keep-mine":
        status = "kept mine"
    elif action == "merge":
        if not drift.is_text:
            raise SmoketreeError(
                f"{drift.authored} is binary; use take-generated or keep-mine."
            )
        merged, conflicts = _merge3(
            drift.base.read_text() if drift.base.exists() else "",
            drift.authored.read_text(),
            drift.template.read_text(),
        )
        drift.authored.write_text(merged)
        status = f"merged ({conflicts} conflict(s))" if conflicts else "merged cleanly"
    else:
        raise SmoketreeError(f"Unknown reconcile action '{action}'.")
    # Advance the fork-base to the current template — the drift is now resolved.
    drift.base.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(drift.template, drift.base)
    return status


def _merge3(base: str, mine: str, theirs: str) -> tuple[str, int]:
    """3-way merge via ``git merge-file`` — returns (merged text, conflict count)."""
    with tempfile.TemporaryDirectory() as d:
        bp, mp, tp = Path(d) / "base", Path(d) / "mine", Path(d) / "theirs"
        bp.write_text(base)
        mp.write_text(mine)
        tp.write_text(theirs)
        try:
            proc = subprocess.run(
                ["git", "merge-file", "-p", "--diff3",
                 "-L", "mine", "-L", "base", "-L", "generated",
                 str(mp), str(bp), str(tp)],
                capture_output=True, text=True,
            )
        except FileNotFoundError as exc:  # pragma: no cover - git missing
            raise SmoketreeError("3-way merge needs `git` on PATH.") from exc
    # git merge-file returns the conflict count, or a negative value (255 here) on error.
    if proc.returncode == 255:
        raise SmoketreeError(f"git merge-file failed: {proc.stderr.strip()}")
    return proc.stdout, proc.returncode
