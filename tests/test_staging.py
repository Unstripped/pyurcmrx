import threading
from pathlib import Path

import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from pyurcmrx import (
    CatalogUnavailableError,
    ManifestRecord,
    core_model_record,
    fetch_core_model,
    parse_manifest,
    rotating_checksum,
    select_manifest,
)

FIXTURES = Path(__file__).parent / "fixtures"
PHONE = (FIXTURES / "core_model_spec53.json").read_bytes()  # system_name "Minimal Interop"
TABLET = (FIXTURES / "core_model_spec30.json").read_bytes()  # system_name "Minimal System"
ZERO_ADDR = "A" + "0" * 16

# Spec §20 §8.2: `ME206_O0000000000000007_T6B49D200.info`, 68 bytes.
SPEC_MANIFEST = bytes.fromhex(
    "30 09 30 09 4D 45 32 30 36 5F 4F 30 30 30 30 30"
    "30 30 30 30 30 30 30 30 30 30 37 5F 43 30 30 30"
    "30 38 44 33 32 5F 6D 6F 64 65 6C 2E 6A 73 6F 6E"
    "09 32 30 09 30 09 38 44 33 32 09 30 09 4E 2F 41"
    "09 30 0D 0A"
)


def staged_name(template: str, data: bytes) -> str:
    return f"T{template}_{ZERO_ADDR}_C{rotating_checksum(data):08X}_core_model.json"


def manifest_name(model: str, time: str = "6B49D200") -> str:
    return f"M{model}_{ZERO_ADDR}_T{time}.info"


def manifest(*files: tuple[str, bytes]) -> bytes:
    return b"".join(
        f"0\t0\t{name}\t{len(data)}\t0\t{rotating_checksum(data):X}\t0\tN/A\t0\r\n".encode() for name, data in files
    )


def test_parse_manifest_spec_vector():
    assert len(SPEC_MANIFEST) == 68
    assert parse_manifest(SPEC_MANIFEST) == (
        ManifestRecord(0, 0, "ME206_O0000000000000007_C00008D32_model.json", 20, 0, 0x8D32, 0),
    )


@pytest.mark.parametrize(
    "data, message",
    [
        (SPEC_MANIFEST[:-2], "not CR LF terminated"),
        (SPEC_MANIFEST.replace(b"\t0\r\n", b"\r\n"), "expected 9 fields, got 8"),
        (SPEC_MANIFEST.replace(b"8D32\t", b"08D32\t"), "malformed number"),
        (SPEC_MANIFEST.replace(b"\t20\t", b"\t-20\t"), "malformed number"),
        (b"\xff\r\n", "not ASCII"),
    ],
)
def test_parse_manifest_rejects_malformed_records(data, message):
    with pytest.raises(CatalogUnavailableError, match=message):
        parse_manifest(data)


def test_select_manifest_takes_only_the_client_models_model_wide_manifest():
    names = [
        manifest_name("E206"),
        "ME207_O0000000000000007_T6B49D200.info",
        staged_name("0002", PHONE),
        f"updateroot/{manifest_name('E207')}",
    ]
    assert select_manifest(names) == manifest_name("E207")
    assert select_manifest(names, "E206") == manifest_name("E206")


@pytest.mark.parametrize(
    "names, message",
    [
        ([staged_name("0002", PHONE)], "no model-wide manifest staged for client model E207"),
        ([manifest_name("E207"), manifest_name("E207", "6B49D201")], "several model-wide manifests"),
    ],
)
def test_select_manifest_rejects_missing_or_ambiguous(names, message):
    with pytest.raises(CatalogUnavailableError, match=message):
        select_manifest(names)


def test_select_manifest_rejects_malformed_client_model():
    with pytest.raises(ValueError, match="4 uppercase hex digits"):
        select_manifest([], "e207")


def test_core_model_record_ignores_other_content():
    records = parse_manifest(manifest(("T0002_A0000000000000000_C0000ABCD_button.json", b"x"), (staged_name("0002", PHONE), PHONE)))
    assert core_model_record(records).staged_name == staged_name("0002", PHONE)


@pytest.mark.parametrize(
    "files, message",
    [
        ([("T0002_A0000000000000000_C0000ABCD_button.json", b"x")], "manifest names no core_model.json"),
        ([(staged_name("0002", PHONE), PHONE), (staged_name("0003", TABLET), TABLET)], "several core models"),
    ],
)
def test_core_model_record_rejects_missing_or_ambiguous(files, message):
    with pytest.raises(CatalogUnavailableError, match=message):
        core_model_record(parse_manifest(manifest(*files)))


@pytest.fixture
def ftp_jail(tmp_path):
    """Anonymous read-only FTP server whose root contains `updateroot/` (spec §10 §3)."""
    (tmp_path / "updateroot").mkdir()
    authorizer = DummyAuthorizer()
    authorizer.add_anonymous(str(tmp_path))
    handler = type("Handler", (FTPHandler,), {"authorizer": authorizer})
    server = FTPServer(("127.0.0.1", 0), handler)
    stop = threading.Event()

    def run():
        # Poll and close on the same thread: closing from another thread races the poller.
        while not stop.is_set():
            server.serve_forever(timeout=0.05, blocking=False)
        server.close_all()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    yield tmp_path / "updateroot", server.address[1]
    stop.set()
    thread.join(2)


async def test_fetch_loads_the_file_named_in_the_client_models_manifest(ftp_jail):
    updateroot, port = ftp_jail
    stale = PHONE.replace(b"Minimal Interop", b"Stale Interop")
    for name, data in [
        (staged_name("0002", PHONE), PHONE),
        (staged_name("0002", stale), stale),  # superseded copy no manifest names (§20 §4.3)
        (staged_name("0003", TABLET), TABLET),
        (manifest_name("E207"), manifest((staged_name("0002", PHONE), PHONE))),
        (manifest_name("E206"), manifest((staged_name("0003", TABLET), TABLET))),
    ]:
        (updateroot / name).write_bytes(data)
    phone = await fetch_core_model("127.0.0.1", port=port)
    tablet = await fetch_core_model("127.0.0.1", client_model="E206", port=port)
    assert (phone.system_name, tablet.system_name) == ("Minimal Interop", "Minimal System")
    assert [(r.id, r.name, [d.id for d in r.devices]) for r in phone.rooms] == [(1, "Living Room", [2])]


@pytest.mark.parametrize(
    "name, listed, message",
    [
        (staged_name("0002", PHONE), PHONE + b" ", "manifest says"),
        (staged_name("0002", PHONE), PHONE.replace(b"Interop", b"Interap"), "does not match manifest"),
        (f"T0002_{ZERO_ADDR}_C00000001_core_model.json", PHONE, "does not match filename token"),
    ],
)
async def test_fetch_rejects_content_that_does_not_match_its_record(ftp_jail, name, listed, message):
    updateroot, port = ftp_jail
    (updateroot / name).write_bytes(PHONE)
    (updateroot / manifest_name("E207")).write_bytes(manifest((name, listed)))
    with pytest.raises(CatalogUnavailableError, match=message):
        await fetch_core_model("127.0.0.1", port=port)


async def test_fetch_ignores_core_models_no_manifest_names(ftp_jail):
    updateroot, port = ftp_jail
    (updateroot / staged_name("0002", PHONE)).write_bytes(PHONE)
    with pytest.raises(CatalogUnavailableError, match="no model-wide manifest staged for client model E207"):
        await fetch_core_model("127.0.0.1", port=port)


async def test_fetch_wraps_connection_failure():
    with pytest.raises(CatalogUnavailableError, match="FTP fetch from 127.0.0.1:1 failed"):
        await fetch_core_model("127.0.0.1", port=1, timeout=1)
