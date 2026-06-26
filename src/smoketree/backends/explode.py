"""Explode backend: fan one input data file's list out into per-item directories.

A declarative, first-class replacement for a hand-written scatter script. The rule's single
input is a data file (YAML/JSON) holding a list; each element is written to the scatter
output — one directory per item. Config:

    items: <key>   # optional — the key holding the array when the input is a mapping. If
                   #   omitted, the document itself must be a list (or a mapping with exactly
                   #   one list-valued key, which is used).
    key: <field>   # optional — an item field to slugify into the scatter directory name;
                   #   defaults to the item's zero-based index (e.g. 000, 001, …).
    protect: <pat> # optional — a path-pattern keyed by the scatter axis (e.g.
                   #   "concepts/{concept}.md"); an item whose protected file exists is
                   #   SKIPPED, never overwriting a human-owned (hand-authored) item.

If the output port declares a schema, every item is validated against it before writing.
"""

from __future__ import annotations

import re

import jsonschema

from ..bind import Pattern
from ..errors import ExecutionError
from ..serde import dump_data, load_data
from .base import Backend, ExecutionContext


def _slugify(value: object) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return s or "item"


class ExplodeBackend(Backend):
    def execute(self, ctx: ExecutionContext) -> None:
        rule = ctx.rule_name
        if len(ctx.inputs) != 1:
            raise ExecutionError(
                f"Rule '{rule}': explode needs exactly one input (got {len(ctx.inputs)})."
            )
        if len(ctx.outputs) != 1:
            raise ExecutionError(
                f"Rule '{rule}': explode needs exactly one output (got {len(ctx.outputs)})."
            )
        ((in_name, in_value),) = ctx.inputs.items()
        ((out_name, _owned),) = ctx.outputs.items()
        if isinstance(in_value, list):
            raise ExecutionError(
                f"Rule '{rule}': explode input '{in_name}' must be a single data file, "
                f"not a list/glob."
            )

        items = self._resolve_items(ctx, in_value)

        pattern_raw = ctx.out_patterns.get(out_name)
        if not pattern_raw:
            raise ExecutionError(f"Rule '{rule}': explode output pattern is missing.")
        pattern = Pattern.compile(pattern_raw)
        axes = [k for k in pattern.keys if k not in ctx.keys]
        if len(axes) != 1:
            raise ExecutionError(
                f"Rule '{rule}': explode output '{out_name}' must have exactly one scatter "
                f"{{key}} (an output key not bound by the input); found {axes or 'none'}."
            )
        axis = axes[0]
        key_field = ctx.config.get("key")
        schema = ctx.schemas.get(out_name)
        protect = ctx.config.get("protect")  # path-pattern; items whose file exists are skipped
        protect_pat = Pattern.compile(protect) if protect else None

        used: set[str] = set()
        for index, item in enumerate(items):
            slug = self._slug_for(ctx, item, key_field, index)
            # Protected slug (a human-authored owner exists): never overwrite it.
            if protect_pat is not None:
                owner = ctx.project.root / protect_pat.fill({**ctx.keys, axis: slug})
                if owner.exists():
                    continue
            base, n = slug, 2
            while slug in used:  # keep dir names unique within this round
                slug, n = f"{base}-{n}", n + 1
            used.add(slug)
            if schema is not None:
                self._validate(ctx, out_name, item, schema, slug)
            rel = pattern.fill({**ctx.keys, axis: slug})
            dump_data(item, ctx.project.root / rel)

    def _resolve_items(self, ctx: ExecutionContext, data_path):
        data = load_data(data_path)
        items_key = ctx.config.get("items")
        if items_key is not None:
            if not isinstance(data, dict) or items_key not in data:
                raise ExecutionError(
                    f"Rule '{ctx.rule_name}': explode config items='{items_key}' not found "
                    f"in input {data_path}."
                )
            data = data[items_key]
        elif isinstance(data, dict):
            list_values = [v for v in data.values() if isinstance(v, list)]
            if len(list_values) == 1:
                data = list_values[0]
            else:
                raise ExecutionError(
                    f"Rule '{ctx.rule_name}': explode input {data_path} is a mapping; set "
                    f"config 'items' to the key holding the array to fan out."
                )
        if not isinstance(data, list):
            raise ExecutionError(
                f"Rule '{ctx.rule_name}': explode expected a list to fan out, got "
                f"{type(data).__name__}."
            )
        return data

    def _slug_for(self, ctx: ExecutionContext, item, key_field, index: int) -> str:
        if key_field is None:
            return f"{index:03d}"
        if not isinstance(item, dict) or key_field not in item:
            raise ExecutionError(
                f"Rule '{ctx.rule_name}': explode config key='{key_field}' is missing on "
                f"item {index}."
            )
        return _slugify(item[key_field])

    def _validate(self, ctx: ExecutionContext, out_name, item, schema, slug) -> None:
        try:
            jsonschema.validate(item, schema)
        except jsonschema.ValidationError as exc:
            raise ExecutionError(
                f"Rule '{ctx.rule_name}': exploded item '{slug}' failed schema validation "
                f"for port '{out_name}': {exc.message} "
                f"(at {'/'.join(str(p) for p in exc.absolute_path) or '<root>'})."
            ) from exc
