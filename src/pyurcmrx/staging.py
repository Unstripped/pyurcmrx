"""Read the client-facing project model from `updateroot/` over anonymous FTP.

Spec §10 §3: anonymous logins land in an FTP jail whose `updateroot/` subdirectory is
bind-mounted to `/remote/Store/updateroot`. Spec §53 §3.4.1 and §20 §4.3: a client loads the
single `core_model.json` named in its own model-wide manifest
`M<client model>_A0000000000000000_T<t>.info`, never a file picked by matching staged names.
The staged name carries `C{CHKSUM}`, the §20 §3 rotating checksum of the content.
"""

from __future__ import annotations

import asyncio
import ftplib
import re
from collections.abc import Iterable
from dataclasses import dataclass

from .checksum import rotating_checksum
from .errors import CatalogUnavailableError
from .project import CoreModel, parse_core_model

FTP_PORT = 21
UPDATEROOT_DIR = "updateroot"
# iPhone client model; its manifest names the T0002 copy in the reference (spec §20 §4.3).
DEFAULT_CLIENT_MODEL = "E207"
CLIENT_MODEL = re.compile(r"^[0-9A-F]{4}$")
CORE_MODEL_NAME = re.compile(r"^T(?P<template>\d{4})_A0{16}_C(?P<checksum>[0-9A-F]{8})_core_model\.json$")
_HEX = re.compile(r"^(0|[1-9A-F][0-9A-F]*)$")  # uppercase hex, no leading zeros
_DECIMAL = re.compile(r"^(0|[1-9][0-9]*)$")


@dataclass(frozen=True, slots=True)
class ManifestRecord:
    """One `.info` manifest record (spec §20 §4.2)."""

    is_archive: int
    archive_ordinal: int
    staged_name: str
    size: int
    version: int
    checksum: int
    payload_checksum: int


def parse_manifest(data: bytes) -> tuple[ManifestRecord, ...]:
    """Parse an ASCII manifest: CR LF terminated records of nine TAB-separated fields."""
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as err:
        raise CatalogUnavailableError(f"manifest is not ASCII: {err}") from err
    if text and not text.endswith("\r\n"):
        raise CatalogUnavailableError("manifest record is not CR LF terminated")
    records = []
    for n, line in enumerate(text.split("\r\n")[:-1]):
        fields = line.split("\t")
        if len(fields) != 9:
            raise CatalogUnavailableError(f"manifest record {n}: expected 9 fields, got {len(fields)}")
        decimals, hexes = (fields[0], fields[1], fields[3]), (fields[4], fields[5], fields[6])
        if not all(_DECIMAL.match(v) for v in decimals) or not all(_HEX.match(v) for v in hexes):
            raise CatalogUnavailableError(f"manifest record {n}: malformed number in {line!r}")
        records.append(
            ManifestRecord(
                is_archive=int(fields[0]),
                archive_ordinal=int(fields[1]),
                staged_name=fields[2],
                size=int(fields[3]),
                version=int(fields[4], 16),
                checksum=int(fields[5], 16),
                payload_checksum=int(fields[6], 16),
            )
        )
    return tuple(records)


def select_manifest(names: Iterable[str], client_model: str = DEFAULT_CLIENT_MODEL) -> str:
    """Pick the client model's model-wide manifest from an `updateroot/` listing."""
    if not CLIENT_MODEL.match(client_model):
        raise ValueError(f"client model must be 4 uppercase hex digits: {client_model!r}")
    pattern = re.compile(rf"^M{client_model}_A0{{16}}_T[0-9A-F]{{8}}\.info$")
    found = sorted(base for name in names if pattern.match(base := name.rsplit("/", 1)[-1]))
    if not found:
        raise CatalogUnavailableError(f"no model-wide manifest staged for client model {client_model}")
    if len(found) > 1:
        raise CatalogUnavailableError(f"several model-wide manifests staged for client model {client_model}: {found}")
    return found[0]


def core_model_record(records: Iterable[ManifestRecord]) -> ManifestRecord:
    """Return the manifest's single `core_model.json` record."""
    found = [r for r in records if CORE_MODEL_NAME.match(r.staged_name)]
    if not found:
        raise CatalogUnavailableError("manifest names no core_model.json")
    if len(found) > 1:
        raise CatalogUnavailableError(f"manifest names several core models: {[r.staged_name for r in found]}")
    return found[0]


def verify_staged_core_model(record: ManifestRecord, data: bytes) -> None:
    """Check the content against the manifest's size and checksum and the name's `C` token."""
    if len(data) != record.size:
        raise CatalogUnavailableError(f"{record.staged_name}: {len(data)} bytes, manifest says {record.size}")
    actual = rotating_checksum(data)
    if actual != record.checksum:
        raise CatalogUnavailableError(
            f"{record.staged_name}: content checksum {actual:X} does not match manifest {record.checksum:X}"
        )
    match = CORE_MODEL_NAME.match(record.staged_name)
    if match is None or actual != int(match["checksum"], 16):
        raise CatalogUnavailableError(
            f"{record.staged_name}: content checksum {actual:08X} does not match filename token"
        )


def _retrieve(ftp: ftplib.FTP, name: str) -> bytes:
    chunks: list[bytes] = []
    ftp.retrbinary(f"RETR {name}", chunks.append)
    return b"".join(chunks)


def _download(host: str, port: int, timeout: float, client_model: str) -> tuple[ManifestRecord, bytes]:
    with ftplib.FTP(timeout=timeout) as ftp:
        ftp.connect(host, port)
        ftp.login()  # anonymous
        ftp.cwd(UPDATEROOT_DIR)
        try:
            names = ftp.nlst()
        except ftplib.error_perm as err:
            # vsftpd and others answer an empty listing with 550/450.
            if not str(err).startswith(("550", "450")):
                raise
            names = []
        record = core_model_record(parse_manifest(_retrieve(ftp, select_manifest(names, client_model))))
        return record, _retrieve(ftp, record.staged_name)


async def fetch_core_model(
    host: str, *, client_model: str = DEFAULT_CLIENT_MODEL, port: int = FTP_PORT, timeout: float = 10.0
) -> CoreModel:
    """Download, verify, and parse the `core_model.json` named in the client model's manifest."""
    try:
        record, data = await asyncio.to_thread(_download, host, port, timeout, client_model)
    except ftplib.all_errors as err:
        raise CatalogUnavailableError(f"FTP fetch from {host}:{port} failed: {err}") from err
    verify_staged_core_model(record, data)
    return parse_core_model(data)
