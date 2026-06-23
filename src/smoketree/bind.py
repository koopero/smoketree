"""Pattern binding — the heart of the PathTree core.

A *pattern* is a path string with ``{key}`` axes and plain ``*`` / ``**`` globs, e.g.
``work/episode/{episode}/segment/*/transcript.txt``. Binding a rule means:

1. Glob the project tree for each input pattern and extract its ``{key}`` values.
2. Classify each input as *scalar* (only keys) or *list* (contains a glob, which
   collapses an axis into a list).
3. Join the inputs as relations — **shared key name = natural join, distinct = product** —
   so each resulting key-tuple is one runnable job. A rule with an input that matched
   nothing yields no bindings (the "inputs present" gate falls out of the join).
4. Render each ``out`` pattern and the ``run`` command for the tuple. An ``out`` key not
   bound by any ``in`` is a *scatter*: the output resolves to the deepest fully-bound
   directory prefix the rule owns, and the command writes the runtime set under it.
"""

from __future__ import annotations

import glob as globlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .errors import ExecutionError, ValidationError
from .models import Rule

def template_pattern(out_pattern: str) -> str:
    """The managed-template path for an authored output ('brief.md' -> 'brief.template.md').

    The generator writes this file; the engine courtesy-copies it to the authored copy
    (the declared ``out`` path) once. The transform only touches the final segment, so
    ``{key}`` axes are preserved.
    """
    p = PurePosixPath(out_pattern)
    return str(p.with_name(f"{p.stem}.template{p.suffix}"))


# A pattern token: {key}, ** or * (everything else is literal).
_TOKEN = re.compile(r"\{(\w+)\}|\*\*|\*")
# A {name} reference in a `run` command template.
_VAR = re.compile(r"\{(\w+)\}")


# --------------------------------------------------------------------------- #
# Pattern compilation
# --------------------------------------------------------------------------- #


@dataclass
class Pattern:
    """A compiled path pattern."""

    raw: str
    keys: list[str]          # named {axes}, in order of appearance
    has_glob: bool           # contains * or ** (-> a list input)
    glob_str: str            # filesystem glob ({key} -> *), for discovery
    regex: re.Pattern[str]   # extracts key values from a matched relative path

    @classmethod
    def compile(cls, raw: str) -> "Pattern":
        keys: list[str] = []
        has_glob = False
        glob_parts: list[str] = []
        regex_parts: list[str] = []
        pos = 0
        for m in _TOKEN.finditer(raw):
            literal = raw[pos : m.start()]
            glob_parts.append(literal)
            regex_parts.append(re.escape(literal))
            tok = m.group(0)
            if tok.startswith("{"):
                key = m.group(1)
                if key in keys:
                    raise ValidationError(
                        f"Pattern '{raw}' repeats key '{{{key}}}'; a key may appear once."
                    )
                keys.append(key)
                glob_parts.append("*")
                regex_parts.append(f"(?P<{key}>[^/]+)")
            elif tok == "**":
                has_glob = True
                glob_parts.append("**")
                regex_parts.append(".*")
            else:  # *
                has_glob = True
                glob_parts.append("*")
                regex_parts.append("[^/]+")
            pos = m.end()
        tail = raw[pos:]
        glob_parts.append(tail)
        regex_parts.append(re.escape(tail))
        return cls(
            raw=raw,
            keys=keys,
            has_glob=has_glob,
            glob_str="".join(glob_parts),
            regex=re.compile("^" + "".join(regex_parts) + "$"),
        )

    def fill(self, keys: dict[str, str]) -> str:
        """Render the pattern with key values substituted (must be glob-free)."""
        def repl(m: re.Match[str]) -> str:
            return keys[m.group(1)]

        return _TOKEN.sub(lambda m: repl(m) if m.group(0).startswith("{") else m.group(0),
                          self.raw)


# --------------------------------------------------------------------------- #
# Globbing into relations
# --------------------------------------------------------------------------- #


@dataclass
class _Row:
    """One row of an input relation: a key assignment and the path(s) it binds."""

    keys: dict[str, str]
    paths: list[Path]


def _match_input(root: Path, pattern: Pattern) -> list[_Row]:
    """Glob ``pattern`` under ``root`` and group matches into relation rows.

    Scalar pattern (no glob): one row per match, ``paths=[path]``.
    List pattern (has a glob): one row per distinct key-tuple, ``paths=[all in group]``.
    """
    matches: list[tuple[dict[str, str], Path]] = []
    for rel in globlib.glob(pattern.glob_str, root_dir=str(root), recursive=True):
        rel = rel.replace("\\", "/")
        m = pattern.regex.match(rel)
        if not m:
            continue
        path = root / rel
        if not path.is_file():
            continue
        matches.append((m.groupdict(), path))

    if not pattern.has_glob:
        return [_Row(keys=k, paths=[p]) for k, p in matches]

    groups: dict[tuple[tuple[str, str], ...], _Row] = {}
    for k, p in matches:
        gk = tuple(sorted(k.items()))
        row = groups.get(gk)
        if row is None:
            row = groups[gk] = _Row(keys=k, paths=[])
        row.paths.append(p)
    for row in groups.values():
        row.paths.sort()
    return list(groups.values())


def _join(relations: list[tuple[str, list[_Row]]]) -> list[dict[str, "list[Path]"]]:
    """Natural-join input relations on shared keys, product on distinct keys.

    Returns a list of bindings; each maps every input name -> its bound path list and
    carries the merged key assignment under the reserved ``__keys__`` entry.
    """
    acc: list[dict] = [{"__keys__": {}}]
    for name, rows in relations:
        nxt: list[dict] = []
        for partial in acc:
            pkeys = partial["__keys__"]
            for row in rows:
                if all(pkeys.get(k) == v for k, v in row.keys.items() if k in pkeys):
                    merged = dict(partial)
                    merged["__keys__"] = {**pkeys, **row.keys}
                    merged[name] = row.paths
                    nxt.append(merged)
        acc = nxt
        if not acc:
            break
    return acc


# --------------------------------------------------------------------------- #
# Bindings
# --------------------------------------------------------------------------- #


@dataclass
class Binding:
    """One runnable job: a rule applied to one key-tuple."""

    rule: Rule
    keys: dict[str, str]
    # input name -> a single Path (scalar) or list[Path] (list/pool input)
    inputs: "dict[str, Path | list[Path]]"
    outputs: dict[str, Path]            # output name -> concrete path or owned dir
    enumerable_outputs: list[Path]      # concrete (non-scatter) outputs, for staleness
    owned_prefixes: list[Path]          # scatter owned dirs, for prune
    command: str | None                 # rendered shell command (None for non-shell)
    schemas: dict[str, Path] = field(default_factory=dict)  # port name -> schema file
    # ambient context inputs (excluded from staleness/present-gate): name -> path list
    context: dict[str, list[Path]] = field(default_factory=dict)
    # the staleness basis for the transform — like `command`, but with context blanked,
    # so a changed context input never restales (set by bind_rule)
    transform_key: str = ""

    @property
    def identity(self) -> str:
        """Stable per-job key: rule name + sorted key-tuple."""
        keypart = ",".join(f"{k}={self.keys[k]}" for k in sorted(self.keys))
        return f"{self.rule.name}({keypart})"

    @property
    def is_scatter(self) -> bool:
        return bool(self.owned_prefixes)

    @property
    def transform_fingerprint(self) -> str:
        """The transform text that gates staleness — the rendered command with context
        blanked (shell) or the backend + its config block (ollama/replicate/...). Context
        inputs are deliberately excluded so reading them never restales the binding."""
        return self.transform_key


def _resolve_context(root: Path, keys: dict[str, str], patterns: dict[str, Pattern]) -> dict[str, list[Path]]:
    """Resolve context patterns for one binding — always a (possibly empty) path list.

    Context never gates: rows are filtered to those consistent with the binding's bound
    keys (a pattern with no shared keys, e.g. a plain ``*`` glob, yields the global set).
    """
    resolved: dict[str, list[Path]] = {}
    for name, pat in patterns.items():
        paths: list[Path] = []
        for row in _match_input(root, pat):
            if all(keys.get(k) == v for k, v in row.keys.items()):
                paths.extend(row.paths)
        resolved[name] = sorted(set(paths))
    return resolved


def bind_rule(root: Path, rule: Rule) -> list[Binding]:
    """Enumerate every runnable binding of ``rule`` against the tree under ``root``."""
    in_patterns = {name: Pattern.compile(p) for name, p in rule.in_.items()}
    # An authored port's *managed* output is its template; the engine seeds the authored
    # copy separately. So the binding targets the template path here.
    out_patterns = {
        name: Pattern.compile(template_pattern(p) if name in rule.author else p)
        for name, p in rule.out.items()
    }
    ctx_patterns = {name: Pattern.compile(p) for name, p in rule.context.items()}
    list_inputs = {name for name, pat in in_patterns.items() if pat.has_glob}
    bound_keys = {k for pat in in_patterns.values() for k in pat.keys}

    relations = [
        (name, _match_input(root, pat)) for name, pat in in_patterns.items()
    ]
    # A rule with no inputs at all runs once (e.g. a pure generator); otherwise the
    # join enumerates tuples and gates on every input being present.
    joins = _join(relations) if relations else [{"__keys__": {}}]

    bindings: list[Binding] = []
    for j in joins:
        keys: dict[str, str] = j["__keys__"]
        inputs: dict[str, Path | list[Path]] = {}
        for name in in_patterns:
            paths = j[name]
            inputs[name] = paths if name in list_inputs else paths[0]

        outputs: dict[str, Path] = {}
        enumerable: list[Path] = []
        owned: list[Path] = []
        for name, pat in out_patterns.items():
            unbound = [k for k in pat.keys if k not in keys]
            if unbound:
                owned_dir = root / _owned_prefix(pat, keys)
                outputs[name] = owned_dir
                owned.append(owned_dir)
            else:
                path = root / pat.fill(keys)
                outputs[name] = path
                enumerable.append(path)

        context = _resolve_context(root, keys, ctx_patterns)
        if rule.run is not None:
            command = _render_command(rule, keys, inputs, outputs, context)
            # Fingerprint with context blanked: a changed context input must not restale.
            transform_key = _render_command(
                rule, keys, inputs, outputs, {name: [] for name in ctx_patterns}
            )
        else:
            command = None
            transform_key = f"{rule.backend}:" + json.dumps(rule.config, sort_keys=True)
        schemas = {port: root / rel for port, rel in rule.schemas.items()}
        bindings.append(
            Binding(
                rule=rule,
                keys=keys,
                inputs=inputs,
                outputs=outputs,
                enumerable_outputs=enumerable,
                owned_prefixes=owned,
                command=command,
                schemas=schemas,
                context=context,
                transform_key=transform_key,
            )
        )
    return bindings


def _owned_prefix(pattern: Pattern, keys: dict[str, str]) -> str:
    """The deepest directory prefix of a scatter output whose keys are all bound."""
    segments = pattern.raw.split("/")
    kept: list[str] = []
    for seg in segments:
        seg_keys = [m.group(1) for m in _VAR.finditer(seg)]
        if any(k not in keys for k in seg_keys) or "*" in seg:
            break
        kept.append(seg)
    rendered = "/".join(kept)
    for k, v in keys.items():
        rendered = rendered.replace(f"{{{k}}}", v)
    return rendered


def _render_command(
    rule: Rule,
    keys: dict[str, str],
    inputs: "dict[str, Path | list[Path]]",
    outputs: dict[str, Path],
    ctx_inputs: "dict[str, list[Path]] | None" = None,
) -> str:
    context: dict[str, str] = dict(keys)
    for name, value in inputs.items():
        paths = value if isinstance(value, list) else [value]
        context[name] = " ".join(str(p) for p in paths)
    for name, paths in (ctx_inputs or {}).items():
        context[name] = " ".join(str(p) for p in paths)
    for name, path in outputs.items():
        context[name] = str(path)

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in context:
            raise ExecutionError(
                f"Rule '{rule.name}': command references unknown variable "
                f"'{{{name}}}'. Known: {', '.join(sorted(context)) or '(none)'}."
            )
        return context[name]

    return _VAR.sub(repl, rule.run)
