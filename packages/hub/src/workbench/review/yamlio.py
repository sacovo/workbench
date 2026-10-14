"""YAML for review files: one document per event, readable in a diff, read back exactly.

Writing uses PyYAML's pure-Python safe dumper (same bytes whether or not libyaml is installed),
multi-line strings as literal blocks, no anchors, keys in the order given. Strings that YAML would
read as something else ('yes', '2026-10-14', '1') are quoted by the dumper.

Reading uses the safe loader (libyaml when present) with the timestamp resolver removed, so a
time stays the string it was written as instead of becoming a datetime.
"""

from __future__ import annotations

import yaml

from workbench.errors import InvalidInput

_BaseLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class _Loader(_BaseLoader):  # type: ignore[misc, valid-type]
    pass


_Loader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in _BaseLoader.yaml_implicit_resolvers.items()
}


class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def _represent_str(dumper: yaml.SafeDumper, data: str):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|" if "\n" in data else None)


_Dumper.add_representer(str, _represent_str)


class _Block(dict):
    """The top-level mapping of a document: always block style, even when its values are scalars."""


def _represent_block(dumper: yaml.SafeDumper, data: _Block):
    return dumper.represent_mapping("tag:yaml.org,2002:map", data.items(), flow_style=False)


_Dumper.add_representer(_Block, _represent_block)


def dump_document(obj: dict) -> str:
    """One YAML document, starting with '---' and ending with a newline. Nested collections that
    hold only scalars are written inline ({role: agent}), everything else as blocks."""
    body = yaml.dump(
        _Block(obj),
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=None,
        width=4096,
    )
    return "---\n" + body


def load_documents(text: str, where: str) -> list:
    try:
        return list(yaml.load_all(text, Loader=_Loader))
    except yaml.YAMLError as exc:
        raise InvalidInput(f"{where}: not valid YAML: {exc}") from None


def load_document(text: str, where: str):
    docs = load_documents(text, where)
    if len(docs) != 1:
        raise InvalidInput(f"{where}: expected one YAML document, found {len(docs)}")
    return docs[0]
