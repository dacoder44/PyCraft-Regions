from io import BytesIO

import nbtlib
import pytest

from mca_editor import FormatError
from mca_editor.nbt_utils import Compound, Document, Int, ListTag, String, Tag


def all_types():
    return Document(
        Compound(
            {
                "byte": Tag(1, -128),
                "short": Tag(2, -32768),
                "int": Tag(3, -2147483648),
                "long": Tag(4, -9223372036854775808),
                "float": Tag(5, 1.25),
                "double": Tag(6, -1.125),
                "bytes": Tag(7, b"\0\x7f\x80\xff"),
                "text": String("NBT"),
                "list": ListTag([String("a"), String("b")], 8),
                "empty_list": ListTag([], 6),
                "nested": Compound({"inner": ListTag([ListTag([Int(9)], 3)], 9)}),
                "ints": Tag(11, [-2147483648, 0, 2147483647]),
                "longs": Tag(12, [-9223372036854775808, 0, 9223372036854775807]),
            }
        ),
        "named root",
    )


def test_all_types_roundtrip_with_independent_library():
    document = all_types()
    data = document.serialize()
    assert Document.parse(data) == document
    external = nbtlib.File.parse(BytesIO(data))
    assert isinstance(external["byte"], nbtlib.Byte)
    assert isinstance(external["ints"], nbtlib.IntArray)
    assert external["empty_list"].subtype == nbtlib.Double
    assert external.root_name == "named root"
    output = BytesIO()
    external.write(output)
    assert output.getvalue() == data
    assert Document.parse(output.getvalue()) == document


@pytest.mark.parametrize(
    "text", ["hello", "a\0b", "こんにちは", "😀🎮", "\ud800", "\udfff", "😀\0é"]
)
def test_java_modified_utf8(text):
    document = Document(Compound({text: String(text)}), text)
    assert Document.parse(document.serialize()) == document
    if "\0" in text:
        assert b"\xc0\x80" in document.serialize()
    if "😀" in text:
        assert b"\xed\xa0\xbd\xed\xb8\x80" in document.serialize()


def test_every_truncation_rejected():
    data = all_types().serialize()
    for index in range(len(data)):
        with pytest.raises(FormatError):
            Document.parse(data[:index])


@pytest.mark.parametrize(
    "raw",
    [
        b"\x0a\0\0\0extra",  # trailing bytes
        b"\x0a\0\0\x63\0\0\0",  # unknown type
        b"\x0a\0\0\x07\0\0\xff\xff\xff\xff\0",  # negative array
        b"\x0a\0\0\x09\0\0\0\0\0\0\x01\0",  # nonempty TAG_End list
        b"\x0a\0\0\x08\0\0\0\x01\xff\0",  # invalid UTF
        b"\x0a\0\0\x01\0\x01a\x01\x01\0\x01a\x02\0",  # duplicate names
        b"\x0a\0\0\x09\0\0\x01\x7f\xff\xff\xff",  # huge list
        b"\x0a\0\0\x0c\0\0\x7f\xff\xff\xff",  # huge array
    ],
)
def test_invalid_nbt(raw):
    with pytest.raises(FormatError):
        Document.parse(raw)


def test_nesting_limits_and_mixed_lists():
    deep = b"\x0a\0\0" * 132 + b"\0" * 132
    with pytest.raises(FormatError, match="nesting"):
        Document.parse(deep)
    with pytest.raises(FormatError, match="mixed"):
        Document(Compound({"bad": ListTag([Int(1)], 8)})).serialize()
    with pytest.raises(FormatError, match="65535"):
        Document(Compound({"bad": String("x" * 65536)})).serialize()


def test_non_java_four_byte_utf8_rejected():
    raw = b"\x0a\0\0\x08\0\0\0\x04" + "😀".encode() + b"\0"
    with pytest.raises(FormatError, match="modified UTF-8"):
        Document.parse(raw)
