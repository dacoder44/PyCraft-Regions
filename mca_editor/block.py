"""Immutable block state values; names are not collapsed into generic air."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .exceptions import FormatError
from .nbt_utils import Compound, String, Tag, require

ID_PATTERN = re.compile(r"[a-z0-9_.-]+:[a-z0-9_./-]+\Z")


@dataclass(frozen=True)
class Block:
    name: str
    properties: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not ID_PATTERN.fullmatch(self.name):
            raise ValueError(f"Expected canonical namespaced block ID, got {self.name!r}")
        props = dict(self.properties)
        if any(
            not isinstance(k, str) or not isinstance(v, str) or not k or not v
            for k, v in props.items()
        ):
            raise ValueError("Block properties must be nonempty string keys and values")
        object.__setattr__(self, "properties", MappingProxyType(props))

    @property
    def key(self) -> tuple[str, tuple[tuple[str, str], ...]]:
        return self.name, tuple(sorted(self.properties.items()))

    @classmethod
    def from_nbt(cls, tag: Tag) -> "Block":
        name = require(tag, "Name", 8).value
        props = {}
        if "Properties" in tag.value:
            properties = require(tag, "Properties", 10)
            for key, value in properties.value.items():
                if value.id != 8:
                    raise FormatError("Palette properties must be NBT strings")
                props[key] = value.value
        try:
            return cls(name, props)
        except ValueError as exc:
            raise FormatError(str(exc)) from exc

    def to_nbt(self) -> Tag:
        result = Compound({"Name": String(self.name)})
        if self.properties:
            result.value["Properties"] = Compound(
                {k: String(v) for k, v in sorted(self.properties.items())}
            )
        return result


AIR = Block("minecraft:air")
