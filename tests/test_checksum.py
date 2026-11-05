import pytest

from pyurcmrx.checksum import rotating_checksum


@pytest.mark.parametrize(
    "data, expected",
    [
        (bytes.fromhex("5552432D4D52582180FF"), 0x00003BB2),
        (b"", 0x00000000),
        (bytes(100), 0x00000000),
        (b"\xff" * 8, 0x0000FEF8),
        (b"\xff" * (1 << 20), 0xFDF00000),
        (b"hello from test suite\n", 0x0000B9AB),
        (b'{"id":7,"ui":"lab"}\n', 0x00008D32),
        (b'{"rooms":[{"name":"Lab","devices":[]}]}\n', 0x00018B39),
    ],
    ids=["urc-mrx", "empty", "zeros", "ff8", "ff-1mib", "hello", "json-id", "json-rooms"],
)
def test_spec_20_vectors(data, expected):
    assert rotating_checksum(data) == expected
