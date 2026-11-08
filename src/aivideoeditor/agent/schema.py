"""Convert pydantic models into compact, self-contained JSON schemas for tools."""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].split("/")[-1]
            resolved = _inline(copy.deepcopy(defs[name]), defs)
            extra = {k: v for k, v in node.items() if k != "$ref"}
            resolved.update(extra)
            return resolved
        out = {}
        for k, v in node.items():
            if k in ("title", "$defs"):
                continue
            out[k] = _inline(v, defs)
        # Collapse Optional[X] (anyOf [X, null]) with a default of None into X:
        # the model may simply omit the field.
        any_of = out.get("anyOf")
        if isinstance(any_of, list) and len(any_of) == 2 and {"type": "null"} in any_of \
                and out.get("default", "missing") is None:
            other = next(a for a in any_of if a != {"type": "null"})
            out.pop("anyOf")
            out.pop("default", None)
            merged = dict(other)
            merged.update(out)
            out = merged
        return out
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


def tool_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    defs = raw.get("$defs", {})
    schema = _inline(raw, defs)
    schema.pop("description", None)
    schema["type"] = "object"
    schema.setdefault("properties", {})
    schema["additionalProperties"] = False
    return schema
