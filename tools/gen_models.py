#!/usr/bin/env python3
"""Generate src/pdigger_factory_sdk/models.py from spec/openapi.json.

Every schema in components/schemas becomes a typed dataclass with a tolerant `from_json` (unknown fields are ignored
and the source object is kept in `raw_json`). Inline objects get their own classes, named in INLINE below. Fields the
recording format may redact (SDK-DESIGN.md section 6) are optional. Run from the repository root:

    python tools/gen_models.py            # rewrite models.py
    python tools/gen_models.py --check    # exit 1 if models.py is not what the spec gives (the tests run this)
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec" / "openapi.json"
OUT = ROOT / "src" / "pdigger_factory_sdk" / "models.py"

# Schema names that read better under another name in Python; the spec name stays as an alias.
RENAME = {"VideoEntry": "Source"}
# Handled elsewhere: errors.ErrorCode carries the API's codes and the SDK-only ones.
SKIP = {"ErrorCode"}
# Inline objects: (parent class, property) -> class name. A property that is an array names its item class.
INLINE = {
    ("ApiRoot", "docs_policy"): "DocsPolicy",
    ("Topology", "wrist"): "TopologyWrist",
    ("Sip1Packet", "maps"): "Sip1Maps",
    ("Sip1Packet", "depth"): "Sip1Depth",
    ("Sip1Packet", "boxes"): "Sip1Boxes",
    ("Sip1Packet", "person"): "Sip1Person",
    ("Sip1Packet", "stones"): "Sip1Stones",
    ("Sip1Packet", "tray"): "Sip1Tray",
    ("Sip1Packet", "hands"): "Sip1Hands",
    ("OverlayLayer", "regions"): "OverlayRegions",
    ("OverlayLayer", "mask"): "OverlayMask",
    ("OverlayLayer", "boxes"): "OverlayBoxes",
    ("OverlayLayer", "wrists"): "OverlayWrists",
    ("OverlayLayer", "items"): "OverlayItem",
    ("SceneLayer", "plane"): "ScenePlane",
    ("SceneLayer", "board"): "SceneBoard",
    ("SceneLayer", "surface"): "SceneSurface",
    ("SceneSurface", "quads"): "SurfaceQuads",
    ("SceneLayer", "skeleton"): "SceneSkeleton",
    ("SceneLayer", "items"): "SceneItem",
    ("CardsLayer", "stage"): "CardsStage",
    ("CardsStage", "parts"): "StagePart",
    ("CardsLayer", "numbers"): "CardsNumbers",
    ("HandCard", "fine"): "FineAction",
    ("WorldLayer", "hands"): "WorldHands",
    ("StoneLook", "at"): "LookFace",
    ("WorldSpeckle", "ref"): "SpeckleRef",
    ("WorldSpeckle", "at"): "SpeckleAt",
    ("WorldLayer", "stats"): "WorldStats",
    ("WorldDraw", "skeleton"): "DrawSkeleton",
    ("EventLog", "events"): "EventLogEntry",
    ("Overlay2D", "skeleton"): "Overlay2DSkeleton",
    ("Scene3D", "skeleton"): "Scene3DSkeleton",
}
# Required by the schema but removed from public recordings (SDK-DESIGN.md section 6), so optional here.
REDACTABLE = {("Sip1Maps", "human"), ("Sip1Depth", "q"), ("SceneSurface", "q")}
# Fields parsed by a hand-written converter: a stone's look that does not fit StoneLook by value is dropped (None), so
# a bad look never makes its stone or frame unreadable (SDK-DESIGN.md 4.6.2).
CONVERTERS = {("StoneView", "look"): "_look_or_none", ("WorldLayer", "speckle"): "_speckle_or_none"}
TAIL = '''

def _look_or_none(data: Any) -> StoneLook | None:
    """A stone's look as `StoneView.look` reads it: None when it does not fit `StoneLook` by value (SDK-DESIGN.md
    4.6.2: numbers by value, never a boolean or a string), so a bad look never makes its stone or frame unreadable."""
    if not fits_look(data):
        return None
    # the whole numbers as their doubles, as every SDK reads them (2^53 + 1 is 2^53)
    return StoneLook.from_json({**data, **{k: int(float(data[k])) for k in ("v", "g", "nu", "nv")}})


def _speckle_or_none(data: Any) -> WorldSpeckle | None:
    """The unit's speckle answer as `WorldLayer.speckle` reads it: None when it does not fit `WorldSpeckle` by value
    (SDK-DESIGN.md 4.6.12), so a bad answer never makes its world or frame unreadable."""
    if not fits_speckle(data):
        return None

    def whole(x: Any) -> Any:
        return None if x is None else int(float(x))

    def find(f: dict[str, Any]) -> dict[str, Any]:
        return {**f, "id": whole(f["id"]), "at": whole(f.get("at"))}

    return WorldSpeckle.from_json(
        {
            **data,
            "v": 1,
            "tray": whole(data["tray"]),
            "ref": {**data["ref"], "g": whole(data["ref"]["g"])},
            "at": {**data["at"], "g": whole(data["at"]["g"])},
            "stones": [find(f) for f in data["stones"]],
            "refs": [find(f) for f in data["refs"]],
            "pairs": [[whole(i) for i in p] for p in data["pairs"]],
        }
    )
'''
# Defaults for absent fields where absence has a meaning.
DEFAULTS = {("Frame", "missing"): "[]", ("WorldLayer", "rev"): "0"}
# a call appended to the parse: Source fills a camera field the entry leaves out from the others
POST = {"Source": "._fill()"}
# Hand-written members added to generated classes.
EXTRAS = {
    "VideoList": '''
    etag: str | None = _field(default=None, compare=False)
    """The reply's `ETag` header (set by `Api.list_videos`; not part of the JSON). Give it back as `if_none_match` to
    read the catalog again only if it changed."""
''',
    "Source": '''
    @property
    def is_live(self) -> bool:
        """True for the live camera (`live` true or `kind` camera): no duration, no seek, rate 1.0."""
        return self.live or self.kind == "camera"

    def _fill(self) -> Source:
        """Fill the camera fields the entry left out from the others, as the other languages do: `kind: camera` alone is
        live and not seekable, `live: true` alone a camera; an entry with none of them is a seekable recording."""
        o = self.raw_json
        if o.get("live") is None:
            self.live = self.kind == "camera"
        if o.get("kind") is None and self.live:
            self.kind = "camera"
        if o.get("seekable") is None:
            self.seekable = not self.live
        return self
''',
    "SurfaceMesh": '''
    def compact(self) -> SceneSurface | None:
        """The `compact` form as the `scene.surface` it is (quantised depth `q`, `lo`, `hi`, `a`, `b`, `flat`,
        `quads`); `None` for the `vertices` form."""
        return SceneSurface.from_json(self.raw_json) if self.form == "compact" else None
''',
}

HEADER = '''"""Typed models of every schema of the pDigger Factory API (`spec/openapi.yaml`, v2.2).

Generated by `tools/gen_models.py` from `spec/openapi.json`; do not edit by hand. Parsing is tolerant: unknown
fields are ignored and kept in `raw_json`, optional fields are `None` when absent, and the fields that public
recordings redact (face keypoints are null joints; appearance masks and depth textures are absent) are optional. Field
names are the API's own names.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as _field  # a model has a field named "field"
from typing import Any

from ._parse import (
    P,
    as_any,
    as_arr,
    as_bool,
    as_int,
    as_num,
    as_obj,
    as_str,
    dict_of,
    fits_look,
    fits_speckle,
    list_of,
)

# Keep every helper referenced, whichever the spec needs.
_HELPERS = (as_any, as_arr, as_bool, as_int, as_num, as_obj, as_str, dict_of, list_of)
'''


def docstring(text: str, indent: str) -> list[str]:
    """A docstring at `indent`, wrapped at 120 columns between words (a word such as `base-decode` stays whole)."""
    text = " ".join(text.split()).replace('"""', "'''").replace("\\", "\\\\")
    one = f'{indent}"""{text}"""'
    if len(one) <= 120:
        return [one]
    body = textwrap.wrap(text, 120 - len(indent) - 6, break_on_hyphens=False)
    if len(body) == 1:
        return [f'{indent}"""{body[0]}"""']
    return [f'{indent}"""{body[0]}', *[indent + line for line in body[1:]], f'{indent}"""']


class Gen:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.schemas = spec["components"]["schemas"]
        self.classes: list[str] = []
        self.done: set[str] = set()
        self.names: list[str] = []

    def cname(self, ref: str) -> str:
        name = ref.split("/")[-1]
        return RENAME.get(name, name)

    def resolve(self, s: dict[str, Any]) -> dict[str, Any]:
        return self.schemas[s["$ref"].split("/")[-1]] if "$ref" in s else s

    # ---- types ------------------------------------------------------------------------------------------------------
    def nullable_inner(self, s: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """The schema without its null alternative, and whether null was allowed."""
        for key in ("oneOf", "anyOf"):
            if key in s:
                alts = [a for a in s[key] if a.get("type") != "null"]
                if len(alts) == 1 and len(alts) < len(s[key]):
                    return alts[0], True
        t = s.get("type")
        if isinstance(t, list) and "null" in t:
            rest = [x for x in t if x != "null"]
            inner = dict(s)
            inner["type"] = rest[0] if len(rest) == 1 else rest
            if "enum" in inner:
                inner["enum"] = [e for e in inner["enum"] if e is not None]
            return inner, True
        return s, False

    def is_model_object(self, s: dict[str, Any]) -> bool:
        if "allOf" in s:
            return True
        return s.get("type") == "object" and "properties" in s and s.get("additionalProperties") is not True

    def scalar(self, s: dict[str, Any]) -> tuple[str, str] | None:
        t = s.get("type")
        if t is None and "const" in s:
            t = {str: "string", int: "integer", float: "number", bool: "boolean"}[type(s["const"])]
        if t == "string":
            return "str", "as_str"
        if t == "integer":
            return "int", "as_int"
        if t == "number":
            return "float", "as_num"
        if t == "boolean":
            return "bool", "as_bool"
        return None

    def elem_type(self, s: dict[str, Any]) -> str:
        """The annotation of an array element or map value that is not parsed one by one."""
        inner, null = self.nullable_inner(s)
        sc = self.scalar(inner)
        if sc is not None:
            base = sc[0]
        elif inner.get("type") == "array":
            base = "list[" + (self.elem_type(inner["items"]) if "items" in inner else "Any") + "]"
        elif inner.get("type") == "object":
            base = "dict[str, Any]"
        else:
            base = "Any"
        return f"{base} | None" if null else base

    def typ(self, s: dict[str, Any], owner: str, prop: str) -> tuple[str, str]:
        """(annotation, converter expression) of a property schema, without nullability."""
        if "$ref" in s:
            if s["$ref"].split("/")[-1] in SKIP:
                return "str", "as_str"  # ErrorCode: a plain string, so that unknown codes pass through
            c = self.cname(s["$ref"])
            return c, f"{c}.from_json"
        sc = self.scalar(s)
        if sc is not None:
            return sc
        t = s.get("type")
        if t == "array":
            items = s.get("items")
            if items is None:
                return "list[Any]", "as_arr"
            if "$ref" in items:
                c = self.cname(items["$ref"])
                return f"list[{c}]", f"list_of({c}.from_json)"
            if self.is_model_object(items):
                c = self.inline(owner, prop, items)
                return f"list[{c}]", f"list_of({c}.from_json)"
            return f"list[{self.elem_type(items)}]", "as_arr"
        if t == "object" or "properties" in s:
            if self.is_model_object(s):
                c = self.inline(owner, prop, s)
                return c, f"{c}.from_json"
            ap = s.get("additionalProperties")
            if isinstance(ap, dict):
                if "$ref" in ap:
                    c = self.cname(ap["$ref"])
                    return f"dict[str, {c}]", f"dict_of({c}.from_json)"
                return f"dict[str, {self.elem_type(ap)}]", "as_obj"
            return "dict[str, Any]", "as_obj"
        return "Any", "as_any"

    def inline(self, owner: str, prop: str, s: dict[str, Any]) -> str:
        key = (owner, prop)
        if key not in INLINE:
            raise SystemExit(f"name the inline object {owner}.{prop} in INLINE")
        name = INLINE[key]
        self.emit(name, s, f"The `{prop}` object of `{owner}`.")
        return name

    # ---- classes ----------------------------------------------------------------------------------------------------
    def props(self, s: dict[str, Any]) -> tuple[dict[str, Any], list[str], str | None]:
        """Properties, required names and the base schema (for allOf with a $ref)."""
        if "allOf" in s:
            props: dict[str, Any] = {}
            req: list[str] = []
            base = None
            for part in s["allOf"]:
                if "$ref" in part:
                    base = part["$ref"].split("/")[-1]
                p, r, _ = self.props(self.resolve(part))
                props.update(p)
                req += r
            return props, req, base
        return s.get("properties") or {}, list(s.get("required") or []), None

    def emit(self, name: str, s: dict[str, Any], doc: str | None = None) -> None:
        if name in self.done:
            return
        self.done.add(name)
        props, required, base_spec = self.props(s)
        own = props
        base = None
        if base_spec is not None:
            base = RENAME.get(base_spec, base_spec)
            self.emit(base, self.schemas[base_spec])
            base_props = self.props(self.schemas[base_spec])[0]
            own = {k: v for k, v in props.items() if k not in base_props}
        fields = []
        parse = []
        for prop, ps in props.items():
            inner, null = self.nullable_inner(ps)
            ann, conv = self.typ(inner, name, prop)
            conv = CONVERTERS.get((name, prop), conv)
            optional = null or prop not in required or (name, prop) in REDACTABLE
            default = DEFAULTS.get((name, prop))
            if default is None and "default" in inner and optional and not null:
                default = json.dumps(inner["default"]).replace("true", "True").replace("false", "False")
            if not optional:
                field_ann, field_def, expr = ann, "", f'p.req("{prop}", {conv})'
            elif default is not None:
                field_ann = ann
                field_def = " = _field(default_factory=list)" if default == "[]" else f" = {default}"
                expr = f'p.opt("{prop}", {conv}, {default})'
            else:
                field_ann, field_def, expr = f"{ann} | None", " = None", f'p.opt("{prop}", {conv})'
            if prop in own:
                desc = (ps.get("description") or "").strip()
                if ps.get("x-availability") == "shipping":
                    desc = (desc + " " if desc else "") + "Shipping: not sent by the 20.20 unit yet."
                fields.append((prop, field_ann, field_def, desc, optional))
            parse.append(f"{prop}={expr}")
        lines = ["@dataclass(slots=True, kw_only=True)", f"class {name}({base}):" if base else f"class {name}:"]
        lines += docstring(doc or (s.get("description") or "").strip() or f"The `{name}` schema.", "    ")
        lines.append("")
        # required fields first (no default), then the rest, each group in spec order
        for prop, ann, default, desc, _ in sorted(fields, key=lambda f: f[4]):
            lines.append(f"    {prop}: {ann}{default}")
            if desc:
                lines += docstring(desc, "    ")
        if base is None:
            lines.append("    raw_json: dict[str, Any] = _field(default_factory=dict, repr=False, compare=False)")
            lines.append('    """The JSON object this was parsed from, unknown fields included."""')
        lines.append("")
        lines.append("    @classmethod")
        lines.append(f"    def from_json(cls, data: Any) -> {name}:")
        lines += docstring(
            f"Parse a `{name}` from decoded JSON; raises `FactoryError` (`decode`) if it does not fit.", " " * 8
        )
        lines.append(f'        p = P(data, "{name}")')
        lines.append("        return cls(")
        lines += [f"            {expr}," for expr in parse]
        lines.append("            raw_json=p.o,")
        lines.append("        )" + POST.get(name, ""))
        if name in EXTRAS:
            lines.append(EXTRAS[name].rstrip())
        self.classes.append("\n".join(lines))
        self.names.append(name)

    def run(self) -> str:
        for name, s in self.schemas.items():
            if name not in SKIP:
                self.emit(RENAME.get(name, name), s)
        names = sorted(set(self.names) | set(RENAME))
        out = [HEADER, "__all__ = [", *[f'    "{n}",' for n in [*names, "SCHEMAS"]], "]"]
        out += ["\n\n" + c for c in self.classes]
        for old, new in RENAME.items():
            out.append(f'\n\n{old} = {new}\n"""The spec\'s name of `{new}`."""')
        out.append("\nSCHEMAS: dict[str, Any] = {")
        out += [f'    "{n}": {RENAME.get(n, n)},' for n in self.schemas if n not in SKIP]
        out.append("}")
        out.append('"""Every schema of `components/schemas` (except `ErrorCode`, see `errors`) by its spec name."""')
        out.append(TAIL.rstrip())
        return "\n".join(out) + "\n"


def main() -> int:
    text = Gen(json.loads(SPEC.read_text(encoding="utf-8"))).run()
    if "--check" in sys.argv:
        if OUT.read_text(encoding="utf-8") != text:
            print("models.py is out of date: run python tools/gen_models.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
