"""CP-S2-04: TLS through the pinned transport. The connection goes to the validated IP, while SNI and certificate
verification still use the original host name, so a certificate for another name is refused."""
from __future__ import annotations

import datetime
import ssl
import tempfile
from pathlib import Path

import pytest
from collector_support import FixtureServer, loopback_guard
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from ttakkari.collector.fetcher import FetchError, SafeFetcher
from ttakkari.collector.netguard import normalize_url


def make_cert(dns_name: str, directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns_name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / f"{dns_name}.crt", directory / f"{dns_name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cert_path, key_path


def client_context(cert_path: Path) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(cert_path))
    return ctx


async def fetch_https(server: FixtureServer, trusted: Path, host: str = "docs.test"):
    f = SafeFetcher(loopback_guard("docs.test"), user_agent="TtakkariCollector/1.0", verify=client_context(trusted))
    try:
        url = normalize_url(server.url("/x", host), allowed_schemes=("https",), allowed_ports=(server.port,))
        return await f.fetch(url, allowed_schemes=("https",), allowed_ports=(server.port,))
    finally:
        await f.aclose()


async def test_https_verifies_the_original_host_name_not_the_pinned_ip():
    with tempfile.TemporaryDirectory() as d:
        cert, key = make_cert("docs.test", Path(d))
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(str(cert), str(key))
        async with FixtureServer(ssl_context=server_ctx) as server:
            server.page("/x", "secure body")
            res = await fetch_https(server, cert)
            assert res.body == b"secure body" and res.final_url.startswith("https://docs.test:")


async def test_certificate_for_another_name_is_refused():
    with tempfile.TemporaryDirectory() as d:
        wrong, key = make_cert("other.test", Path(d))
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(str(wrong), str(key))
        async with FixtureServer(ssl_context=server_ctx) as server:
            server.page("/x", "must not be delivered")
            with pytest.raises(FetchError) as err:
                await fetch_https(server, wrong)  # trusted, but issued for other.test while we asked for docs.test
            assert err.value.code == "connect_failed" and err.value.retryable
            assert server.hits == []


async def test_untrusted_certificate_is_refused_with_default_trust_store():
    with tempfile.TemporaryDirectory() as d:
        cert, key = make_cert("docs.test", Path(d))
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(str(cert), str(key))
        async with FixtureServer(ssl_context=server_ctx) as server:
            server.page("/x", "x")
            f = SafeFetcher(loopback_guard("docs.test"), user_agent="TtakkariCollector/1.0")  # default verification
            try:
                url = normalize_url(server.url("/x"), allowed_schemes=("https",), allowed_ports=(server.port,))
                with pytest.raises(FetchError) as err:
                    await f.fetch(url, allowed_schemes=("https",), allowed_ports=(server.port,))
            finally:
                await f.aclose()
            assert err.value.code == "connect_failed" and server.hits == []
