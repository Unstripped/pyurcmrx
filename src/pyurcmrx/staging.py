"""Read the client-facing project model from `updateroot/` over anonymous FTP.

Spec §10 §3: anonymous logins land in an FTP jail whose `updateroot/` subdirectory is
bind-mounted to `/remote/Store/updateroot`. Spec §53 §3.4.1: one `core_model.json` per
project is staged there as `T{TEMPLATE_ID}_A0000000000000000_C{CHKSUM}_core_model.json`,
replicated per UI template; `C{CHKSUM}` is the §20 rotating checksum of the content.
"""

from __future__ import annotations

import asyncio
import ftplib
import re
from collections.abc import Iterable

from .checksum import rotating_checksum
from .errors import CatalogUnavailableError
from .project import CoreModel, parse_core_model

FTP_PORT = 21
UPDATEROOT_DIR = "updateroot"
CORE_MODEL_NAME = re.compile(r"^T(?P<template>\d{4})_A0{16}_C(?P<checksum>[0-9A-F]{8})_core_model\.json$")
# Every template carries the same project model; prefer the mobile/standard one.
TEMPLATE_PREFERENCE = ("0002", "0003")


def select_core_model(names: Iterable[str]) -> str:
    """Pick the staged core model filename from an `updateroot/` listing."""
    by_template: dict[str, list[str]] = {}
    for name in names:
        match = CORE_MODEL_NAME.match(name.rsplit("/", 1)[-1])
        if match:
            by_template.setdefault(match["template"], []).append(match.string)
    if not by_template:
        raise CatalogUnavailableError("no *_core_model.json staged in updateroot/")
    ordered = [t for t in TEMPLATE_PREFERENCE if t in by_template] + sorted(set(by_template) - set(TEMPLATE_PREFERENCE))
    candidates = by_template[ordered[0]]
    if len(candidates) > 1:
        raise CatalogUnavailableError(f"several core models staged for template {ordered[0]}: {sorted(candidates)}")
    return candidates[0]


def verify_staged_checksum(name: str, data: bytes) -> None:
    match = CORE_MODEL_NAME.match(name)
    if match is None:
        raise CatalogUnavailableError(f"not a staged core model filename: {name}")
    actual = rotating_checksum(data)
    if actual != int(match["checksum"], 16):
        raise CatalogUnavailableError(f"{name}: content checksum {actual:08X} does not match filename token")


def _download(host: str, port: int, timeout: float) -> tuple[str, bytes]:
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
        name = select_core_model(names)
        chunks: list[bytes] = []
        ftp.retrbinary(f"RETR {name}", chunks.append)
    return name, b"".join(chunks)


async def fetch_core_model(host: str, *, port: int = FTP_PORT, timeout: float = 10.0) -> CoreModel:
    """Download, verify, and parse the controller's staged `core_model.json`."""
    try:
        name, data = await asyncio.to_thread(_download, host, port, timeout)
    except ftplib.all_errors as err:
        raise CatalogUnavailableError(f"FTP fetch from {host}:{port} failed: {err}") from err
    verify_staged_checksum(name, data)
    return parse_core_model(data)
