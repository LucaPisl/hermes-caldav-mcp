"""Loopback-only TLS boundary checks with a disposable synthetic certificate."""

import asyncio
import ipaddress
import socket
import ssl
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from hermes_caldav_mcp.transport import CalDAVClient
from hermes_caldav_mcp.types import SafeError


@pytest.mark.parametrize("case", ["untrusted", "private-ca", "wrong-host"])
async def test_verified_tls_before_authentication(profile, tmp_path, monkeypatch, case):
    address = ipaddress.ip_address(socket.gethostbyname("localhost"))
    assert address.is_loopback
    loopback = str(address)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic loopback test")]
    )
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(address)]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert = tmp_path / "synthetic-cert.pem"
    private = tmp_path / "synthetic-key.pem"
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, private)
    requests = []

    async def handle(reader, writer):
        try:
            requests.append(await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2))
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, loopback, 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    host = "localhost" if case == "wrong-host" else loopback
    p = replace(
        profile,
        base_url=f"https://{host}:{port}/nextcloud",
        ca_bundle=str(cert) if case != "untrusted" else None,
    )
    monkeypatch.setenv("HTTPS_PROXY", f"http://{loopback}:1")
    try:
        async with server:
            async with CalDAVClient(p) as client:
                if case == "private-ca":
                    assert (
                        await client.request("GET", profile.calendars[0].href + "a.ics")
                    ).data == b"ok"
                else:
                    with pytest.raises(SafeError) as caught:
                        await client.request("GET", profile.calendars[0].href + "a.ics")
                    assert caught.value.code.value == "NETWORK_ERROR"
        assert len(requests) == (1 if case == "private-ca" else 0)
        if requests:
            assert b"Authorization: Basic " in requests[0]
    finally:
        server.close()
        await server.wait_closed()
