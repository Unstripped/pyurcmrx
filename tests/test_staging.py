import threading
from pathlib import Path

import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from pyurcmrx import CatalogUnavailableError, fetch_core_model, rotating_checksum, select_core_model

FIXTURE = (Path(__file__).parent / "fixtures" / "core_model_spec53.json").read_bytes()
ZERO_ADDR = "A" + "0" * 16


def staged_name(template: str, data: bytes) -> str:
    return f"T{template}_{ZERO_ADDR}_C{rotating_checksum(data):08X}_core_model.json"


def test_select_prefers_template_0002_and_ignores_other_files():
    names = [staged_name("0003", b"x"), "M8008_M001FB80011220000_T632B7C9D.info", staged_name("0002", b"y")]
    assert select_core_model(names) == staged_name("0002", b"y")


@pytest.mark.parametrize(
    "names, message",
    [
        ([], "no *_core_model.json"),
        ([staged_name("0002", b"a"), staged_name("0002", b"b")], "several core models staged for template 0002"),
    ],
)
def test_select_rejects_missing_or_ambiguous(names, message):
    with pytest.raises(CatalogUnavailableError, match=message.replace("*", r"\*")):
        select_core_model(names)


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


async def test_fetch_downloads_verifies_and_parses(ftp_jail):
    updateroot, port = ftp_jail
    (updateroot / staged_name("0002", FIXTURE)).write_bytes(FIXTURE)
    (updateroot / staged_name("0003", FIXTURE)).write_bytes(FIXTURE)
    model = await fetch_core_model("127.0.0.1", port=port)
    assert [(r.id, r.name, [d.id for d in r.devices]) for r in model.rooms] == [(1, "Living Room", [2])]


async def test_fetch_rejects_checksum_mismatch(ftp_jail):
    updateroot, port = ftp_jail
    (updateroot / f"T0002_{ZERO_ADDR}_C00000001_core_model.json").write_bytes(FIXTURE)
    with pytest.raises(CatalogUnavailableError, match="does not match filename token"):
        await fetch_core_model("127.0.0.1", port=port)


async def test_fetch_reports_empty_updateroot(ftp_jail):
    _, port = ftp_jail
    with pytest.raises(CatalogUnavailableError, match="no \\*_core_model.json"):
        await fetch_core_model("127.0.0.1", port=port)


async def test_fetch_wraps_connection_failure():
    with pytest.raises(CatalogUnavailableError, match="FTP fetch from 127.0.0.1:1 failed"):
        await fetch_core_model("127.0.0.1", port=1, timeout=1)
