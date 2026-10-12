import random

import pytest

from mca_editor import FormatError
from mca_editor.packed_array import bit_width, long_count, pack, unpack


def independent_pack(values, bits, padded):
    """Slow bit-by-bit reference; no shared production offsets/packing code."""
    stream = []
    if padded:
        for start in range(0, len(values), 64 // bits):
            segment = []
            for value in values[start : start + 64 // bits]:
                segment.extend((value >> bit) & 1 for bit in range(bits))
            stream.extend(segment + [0] * (64 - len(segment)))
    else:
        for value in values:
            stream.extend((value >> bit) & 1 for bit in range(bits))
        stream.extend([0] * (-len(stream) % 64))
    result = []
    for start in range(0, len(stream), 64):
        word = sum(bit << i for i, bit in enumerate(stream[start : start + 64]))
        result.append(word if word < 2**63 else word - 2**64)
    return result


@pytest.mark.parametrize("bits", range(4, 13))
@pytest.mark.parametrize("padded", [False, True])
def test_independent_packing_all_boundaries(bits, padded):
    rng = random.Random(123 + bits)
    values = [rng.randrange(1 << bits) for _ in range(4096)]
    words = pack(values, bits, padded=padded)
    assert words == independent_pack(values, bits, padded)
    assert unpack(words, bits, padded=padded) == values
    assert len(words) == long_count(4096, bits, padded)


def test_known_cross_long_vector():
    values = [0] * 4096
    values[12] = 31  # 5 bits at offset 60: four in word 0, one in word 1
    assert pack(values, 5, padded=False)[:2] == [-1152921504606846976, 1]
    assert pack(values, 5, padded=True)[:2] == [0, 31]


@pytest.mark.parametrize(
    "size,bits", [(1, 4), (16, 4), (17, 5), (32, 5), (33, 6), (256, 8), (257, 9), (4096, 12)]
)
def test_width(size, bits):
    assert bit_width(size) == bits


def test_invalid_arrays():
    with pytest.raises(FormatError):
        unpack([0], 5, padded=True)
    with pytest.raises(FormatError):
        pack([32], 5, padded=False)
    for count in (0, 4097):
        with pytest.raises(FormatError):
            bit_width(count)
