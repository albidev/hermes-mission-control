"""One owner identity per installation/profile (P3b.1a-1; design section 1 "Identity", amendment R1-04).

Layout under the explicit, required, private ``identity_dir`` (0700, owner UID, opened no-follow)::

    identity_dir/
      .identity.lock          lock file, OUTSIDE the bundle (flock; fstat regular/UID/0600)
      .staging-<random>/      bundle under construction (swept by the next initializer, never published as-is)
      current/                the ONLY thing readers look at; published by ONE rename of the finished staging dir
        consent.key           raw Ed25519 seed (the existing ``ConsentKey`` file format)
        identity.json         {"version":1,"installation_id":...,"profile":...}
        tls/ca.key ca.pem leaf.key leaf.pem

Initialisation: lock -> (``current`` exists? validate it, never repair) -> build everything in ``.staging-*``
(every file fsynced, then ``tls/`` and the staging dir) -> ONE ``rename`` to ``current`` -> fsync the parent.
A crash therefore leaves no ``current`` or a complete one.  Readers validate the WHOLE set: exact file names,
type/UID/0600 (0700 for dirs) without following links, key/certificate coherence, the strict X.509 profile
ported from the spike ``pki.py`` (leaf validity <= 1 year, no SAN, serverAuth+clientAuth), expiry, and that
``identity.json`` equals the installation/profile the caller read from config and the native gateway.
Partial / foreign / symlinked / over-broad objects are refused, never repaired.  Errors are generic codes.
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, NoReturn

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from invite_rooms import private_fs as fs
from invite_rooms.pairing_codec import PeerIdentity

IDENTITY_VERSION = 1
CURRENT = "current"
LOCK_NAME = ".identity.lock"
STAGING_PREFIX = ".staging-"
CONSENT_KEY, IDENTITY_JSON, TLS_DIR = "consent.key", "identity.json", "tls"
TLS_FILES = ("ca.key", "ca.pem", "leaf.key", "leaf.pem")
LEAF_DAYS = 365
CA_DAYS = 1825
KEY_BITS = 2048
BACKDATE = _dt.timedelta(minutes=5)
MAX_FILE_BYTES = 16 * 1024
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.\-]{0,127}")

E_MISSING = "identity_missing"
E_STORAGE = "identity_private_storage_required"
E_INVALID = "identity_invalid"
E_MISMATCH = "identity_mismatch"
E_EXPIRED = "identity_expired"

_FAULT: Callable[[str], None] | None = None  # test seam: called at every durable step; may raise to simulate a crash


class IdentityError(ValueError):
    """Sanitized: ``str(exc)`` is one of the ``identity_*`` codes, never a path, key or certificate."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise IdentityError(code) from None


def _fault(point: str) -> None:
    if _FAULT is not None:
        _FAULT(point)


def _new_rsa_key() -> rsa.RSAPrivateKey:  # seam for tests (key generation dominates the runtime)
    return rsa.generate_private_key(public_exponent=65537, key_size=KEY_BITS)


# ---------------------------------------------------------------------------------------------------------------
# certificates (ported from spikes/invite-rooms/pki.py: same strict X.509 profile, no import from the spike)
# ---------------------------------------------------------------------------------------------------------------
def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _build(*, subject: str, public_key, issuer: x509.Name, issuer_key, issuer_ski: x509.SubjectKeyIdentifier,
           days: int, ca: bool, now: _dt.datetime, eku: list | None = None) -> x509.Certificate:
    builder = (x509.CertificateBuilder()
               .subject_name(_name(subject)).issuer_name(issuer).public_key(public_key)
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - BACKDATE).not_valid_after(now + _dt.timedelta(days=days))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=not ca, content_commitment=False,
                                            key_encipherment=not ca, data_encipherment=False,
                                            key_agreement=False, key_cert_sign=ca, crl_sign=ca,
                                            encipher_only=False, decipher_only=False), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(issuer_ski),
                              critical=False))
    if eku:
        builder = builder.add_extension(x509.ExtendedKeyUsage(eku), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def _key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _cert_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def leaf_fingerprint(leaf: x509.Certificate) -> str:
    return hashlib.sha256(leaf.public_bytes(serialization.Encoding.DER)).hexdigest()


def _validate_pki(ca_key, ca, leaf_key, leaf, now: _dt.datetime) -> None:
    """Strict profile + coherence. Raises IdentityError(invalid|expired)."""
    try:
        ca_bc = ca.extensions.get_extension_for_class(x509.BasicConstraints)
        ca_ku = ca.extensions.get_extension_for_class(x509.KeyUsage)
        ca_ski = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
        ca_aki = ca.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
        lf_bc = leaf.extensions.get_extension_for_class(x509.BasicConstraints)
        lf_ku = leaf.extensions.get_extension_for_class(x509.KeyUsage)
        lf_eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        lf_aki = leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
        leaf.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
        if not (ca_bc.critical and ca_bc.value.ca and ca_bc.value.path_length == 0
                and ca_ku.critical and ca_ku.value.key_cert_sign and ca_ku.value.crl_sign
                and not ca_ku.value.digital_signature
                and ca_aki.key_identifier == ca_ski.digest and ca.issuer == ca.subject
                and lf_bc.critical and not lf_bc.value.ca and lf_bc.value.path_length is None
                and lf_ku.critical and lf_ku.value.digital_signature and lf_ku.value.key_encipherment
                and not lf_ku.value.key_cert_sign and not lf_ku.value.crl_sign
                and sorted(o.dotted_string for o in lf_eku) == sorted(
                    o.dotted_string for o in (ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH))
                and lf_aki.key_identifier == ca_ski.digest and leaf.issuer == ca.subject):
            _fail(E_INVALID)
        try:
            leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        except x509.ExtensionNotFound:
            pass
        else:
            _fail(E_INVALID)  # owner leaf carries no SAN (pinned by fingerprint, not by name)
        leaf.verify_directly_issued_by(ca)
        ca.verify_directly_issued_by(ca)
        if (ca.public_key().public_numbers() != ca_key.public_key().public_numbers()
                or leaf.public_key().public_numbers() != leaf_key.public_key().public_numbers()
                or ca.public_key().key_size < KEY_BITS or leaf.public_key().key_size < KEY_BITS
                or not isinstance(leaf.signature_hash_algorithm, hashes.SHA256)):
            _fail(E_INVALID)
        span = leaf.not_valid_after_utc - leaf.not_valid_before_utc
        if span > _dt.timedelta(days=LEAF_DAYS) + BACKDATE or leaf.not_valid_after_utc > ca.not_valid_after_utc:
            _fail(E_INVALID)
        if leaf.not_valid_before_utc > now + BACKDATE or ca.not_valid_before_utc > now + BACKDATE:
            _fail(E_INVALID)
        if now > leaf.not_valid_after_utc or now > ca.not_valid_after_utc:
            _fail(E_EXPIRED)
    except IdentityError:
        raise
    except (x509.ExtensionNotFound, InvalidSignature, ValueError, TypeError, AttributeError):
        _fail(E_INVALID)


# ---------------------------------------------------------------------------------------------------------------
# bundle value
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class IdentityBundle:
    installation_id: str
    profile: str
    consent_public_key: bytes
    ca_pem: str
    leaf_fingerprint: str
    leaf_not_after: int  # epoch seconds
    directory: Path  # ``<identity_dir>/current``
    _consent_key: Ed25519PrivateKey = field(repr=False, compare=False)

    def consent_key(self) -> Ed25519PrivateKey:
        return self._consent_key

    @property
    def ca_file(self) -> Path:
        return self.directory / TLS_DIR / "ca.pem"

    @property
    def leaf_cert_file(self) -> Path:
        return self.directory / TLS_DIR / "leaf.pem"

    @property
    def leaf_key_file(self) -> Path:
        return self.directory / TLS_DIR / "leaf.key"

    def peer_identity(self, catalog) -> PeerIdentity:
        """The public identity a pairing code carries. ``catalog`` comes from the native gateway, not from here."""
        return PeerIdentity(self.installation_id, self.profile, catalog, self.consent_public_key, self.ca_pem,
                            self.leaf_fingerprint)


def _check_name(value) -> str:
    if type(value) is not str or not _NAME.fullmatch(value):
        _fail(E_INVALID)
    return value


def _identity_json(installation_id: str, profile: str) -> bytes:
    return json.dumps({"version": IDENTITY_VERSION, "installation_id": installation_id, "profile": profile},
                      sort_keys=True, separators=(",", ":")).encode()


# ---------------------------------------------------------------------------------------------------------------
# reader
# ---------------------------------------------------------------------------------------------------------------
def _read_member(dirfd: int, name: str) -> bytes:
    fd = fs.open_regular(dirfd, name)
    try:
        return fs.read_all(fd, MAX_FILE_BYTES)
    finally:
        os.close(fd)


def _open_subdir(dirfd: int, name: str, *, missing: str) -> int:
    try:
        fd = os.open(name, fs.DIR_FLAGS, dir_fd=dirfd)
    except FileNotFoundError:
        _fail(missing)
    except OSError:
        fs.refuse()
    try:
        fs.check_dir(fd, private=True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _check_lock(rootfd: int) -> None:
    """The lock lives outside the bundle but is held to the same bar; absent is fine for a reader."""
    try:
        os.stat(LOCK_NAME, dir_fd=rootfd, follow_symlinks=False)
    except FileNotFoundError:
        return
    os.close(fs.open_regular(rootfd, LOCK_NAME))


def _read_current(rootfd: int, directory: Path, *, installation_id: str, profile: str,
                  now: _dt.datetime) -> IdentityBundle:
    _check_lock(rootfd)
    cfd = _open_subdir(rootfd, CURRENT, missing=E_MISSING)
    tfd = None
    try:
        if set(os.listdir(cfd)) != {CONSENT_KEY, IDENTITY_JSON, TLS_DIR}:
            fs.refuse()
        tfd = _open_subdir(cfd, TLS_DIR, missing=E_INVALID)
        if set(os.listdir(tfd)) != set(TLS_FILES):
            fs.refuse()
        raw_identity = _read_member(cfd, IDENTITY_JSON)
        seed = _read_member(cfd, CONSENT_KEY)
        blobs = {name: _read_member(tfd, name) for name in TLS_FILES}
    finally:
        if tfd is not None:
            os.close(tfd)
        os.close(cfd)
    try:
        meta = json.loads(raw_identity)
    except ValueError:
        _fail(E_INVALID)
    if (type(meta) is not dict or set(meta) != {"version", "installation_id", "profile"}
            or type(meta["version"]) is not int or meta["version"] != IDENTITY_VERSION
            or raw_identity != _identity_json(_check_name(meta["installation_id"]), _check_name(meta["profile"]))):
        _fail(E_INVALID)
    if meta["installation_id"] != installation_id or meta["profile"] != profile:
        _fail(E_MISMATCH)
    if len(seed) != 32:
        _fail(E_INVALID)
    try:
        consent = Ed25519PrivateKey.from_private_bytes(seed)
        ca = x509.load_pem_x509_certificate(blobs["ca.pem"])
        leaf = x509.load_pem_x509_certificate(blobs["leaf.pem"])
        ca_key = serialization.load_pem_private_key(blobs["ca.key"], password=None)
        leaf_key = serialization.load_pem_private_key(blobs["leaf.key"], password=None)
    except (ValueError, TypeError):
        _fail(E_INVALID)
    if (not isinstance(ca_key, rsa.RSAPrivateKey) or not isinstance(leaf_key, rsa.RSAPrivateKey)
            or _cert_pem(ca) != blobs["ca.pem"] or _cert_pem(leaf) != blobs["leaf.pem"]
            or _key_pem(ca_key) != blobs["ca.key"] or _key_pem(leaf_key) != blobs["leaf.key"]):
        _fail(E_INVALID)  # trailing junk, foreign encodings, wrong key types
    _validate_pki(ca_key, ca, leaf_key, leaf, now)
    public = consent.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return IdentityBundle(installation_id, profile, public, blobs["ca.pem"].decode("ascii"), leaf_fingerprint(leaf),
                          int(leaf.not_valid_after_utc.timestamp()), directory / CURRENT, consent)


def _now(now) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(time.time() if now is None else now, _dt.timezone.utc)


def load_identity(identity_dir: str | Path, *, installation_id: str, profile: str, now: float | None = None,
                  anchor: str | Path | None = None) -> IdentityBundle:
    """Read ``identity_dir/current`` only; never creates or repairs anything."""
    _check_name(installation_id); _check_name(profile)
    root = Path(identity_dir)
    if not os.path.lexists(root):
        _fail(E_MISSING)
    try:
        rootfd = fs.open_private_dir(root, Path(anchor) if anchor is not None else None, create=False)
        try:
            return _read_current(rootfd, root, installation_id=installation_id, profile=profile, now=_now(now))
        finally:
            os.close(rootfd)
    except (fs.PrivateStorageError, OSError):
        _fail(E_STORAGE)


# ---------------------------------------------------------------------------------------------------------------
# initializer
# ---------------------------------------------------------------------------------------------------------------
def _write_new(dirfd: int, label: str, name: str, data: bytes) -> None:
    fd = fs.open_regular(dirfd, name, create=True)
    try:
        fs.write_all(fd, data)
        _fault(f"after_write:{label}")
        fs.fsync(fd)
        _fault(f"after_fsync:{label}")
    finally:
        os.close(fd)


def _sweep_staging(rootfd: int) -> None:
    for name in os.listdir(rootfd):
        if name in (CURRENT, LOCK_NAME):
            continue
        if not name.startswith(STAGING_PREFIX):
            fs.refuse()  # foreign entry in the identity dir: refuse, do not touch
        st = os.stat(name, dir_fd=rootfd, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            fs.refuse()
        shutil.rmtree(name, dir_fd=rootfd)


def _build_staging(rootfd: int, name: str, *, installation_id: str, profile: str, now: _dt.datetime) -> None:
    ca_key, leaf_key = _new_rsa_key(), _new_rsa_key()
    ca_name = _name("mc-rooms-ca")
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca = _build(subject="mc-rooms-ca", public_key=ca_key.public_key(), issuer=ca_name, issuer_key=ca_key,
                issuer_ski=ca_ski, days=CA_DAYS, ca=True, now=now)
    leaf = _build(subject="mc-rooms-peer", public_key=leaf_key.public_key(), issuer=ca_name, issuer_key=ca_key,
                  issuer_ski=ca_ski, days=LEAF_DAYS, ca=False, now=now,
                  eku=[ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH])
    _validate_pki(ca_key, ca, leaf_key, leaf, now)  # never publish what the reader would refuse
    consent = Ed25519PrivateKey.generate()
    seed = consent.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption())
    os.mkdir(name, 0o700, dir_fd=rootfd)
    sfd = os.open(name, fs.DIR_FLAGS, dir_fd=rootfd)
    tfd = None
    try:
        os.fchmod(sfd, 0o700)
        fs.check_dir(sfd, private=True)
        os.mkdir(TLS_DIR, 0o700, dir_fd=sfd)
        tfd = os.open(TLS_DIR, fs.DIR_FLAGS, dir_fd=sfd)
        os.fchmod(tfd, 0o700)
        fs.check_dir(tfd, private=True)
        _write_new(sfd, CONSENT_KEY, CONSENT_KEY, seed)
        _write_new(tfd, "tls/ca.key", "ca.key", _key_pem(ca_key))
        _write_new(tfd, "tls/ca.pem", "ca.pem", _cert_pem(ca))
        _write_new(tfd, "tls/leaf.key", "leaf.key", _key_pem(leaf_key))
        _write_new(tfd, "tls/leaf.pem", "leaf.pem", _cert_pem(leaf))
        _write_new(sfd, IDENTITY_JSON, IDENTITY_JSON, _identity_json(installation_id, profile))
        fs.fsync(tfd)
        _fault("after_fsync:tls_dir")
        fs.fsync(sfd)
        _fault("after_fsync:staging_dir")
    finally:
        if tfd is not None:
            os.close(tfd)
        os.close(sfd)


def initialize_identity(identity_dir: str | Path, *, installation_id: str, profile: str, now: float | None = None,
                        anchor: str | Path | None = None) -> IdentityBundle:
    """Create the bundle once (idempotent): an existing ``current`` is validated and returned, never replaced."""
    _check_name(installation_id); _check_name(profile)
    root = Path(identity_dir)
    moment = _now(now)
    try:
        rootfd = fs.open_private_dir(root, Path(anchor) if anchor is not None else None, create=True)
    except (fs.PrivateStorageError, OSError):
        _fail(E_STORAGE)
    try:
        lock_fd = fs.open_regular(rootfd, LOCK_NAME, create=True)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                os.stat(CURRENT, dir_fd=rootfd, follow_symlinks=False)
                present = True
            except FileNotFoundError:
                present = False
            if present:
                return _read_current(rootfd, root, installation_id=installation_id, profile=profile, now=moment)
            _sweep_staging(rootfd)
            staging = f"{STAGING_PREFIX}{secrets.token_hex(8)}"
            try:
                _build_staging(rootfd, staging, installation_id=installation_id, profile=profile, now=moment)
                _fault("before_rename")
                os.rename(staging, CURRENT, src_dir_fd=rootfd, dst_dir_fd=rootfd)
                _fault("after_rename")
                fs.fsync(rootfd)
                _fault("after_parent_fsync")
            except Exception:
                try:  # a real error (not a simulated crash) leaves nothing unpublished behind
                    shutil.rmtree(staging, dir_fd=rootfd)
                except OSError:
                    pass
                raise
            return _read_current(rootfd, root, installation_id=installation_id, profile=profile, now=moment)
        finally:
            os.close(lock_fd)  # releases the flock
    except (fs.PrivateStorageError, OSError):
        _fail(E_STORAGE)
    finally:
        os.close(rootfd)
