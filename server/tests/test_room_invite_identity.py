"""P3b.1a-1: identity bundle (R1-04) -- staged build, single-rename publication, whole-set validation."""
from __future__ import annotations

import datetime as dt
import itertools
import json
import os
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID

from invite_rooms import identity as ident
from invite_rooms import pairing_codec as pc
from invite_rooms import private_fs as fs
from invite_rooms.consent import ConsentKey
from invite_rooms.identity import IdentityError, initialize_identity, load_identity

INSTALL, PROFILE = "install:" + "a" * 32, "default"
SERVER_DIR = str(Path(__file__).resolve().parents[1])
_REAL_RSA = ident._new_rsa_key
_POOL = [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(3)]
CODES = {ident.E_MISSING, ident.E_STORAGE, ident.E_INVALID, ident.E_MISMATCH, ident.E_EXPIRED}


class Crash(BaseException):
    """Simulated power loss: not an Exception, so no cleanup handler may run."""


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=os.environ.get("MC_TEST_TMP")))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dir = self.tmp / "identity"
        keys = itertools.cycle(_POOL)
        patcher = patch.object(ident, "_new_rsa_key", side_effect=lambda: next(keys))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, ident, "_FAULT", None)

    def init(self, **kw):
        kw.setdefault("installation_id", INSTALL); kw.setdefault("profile", PROFILE)
        return initialize_identity(self.dir, anchor=self.tmp, **kw)

    def load(self, **kw):
        kw.setdefault("installation_id", INSTALL); kw.setdefault("profile", PROFILE)
        return load_identity(self.dir, anchor=self.tmp, **kw)

    def assertCode(self, code, fn, *args, **kwargs):
        with self.assertRaises(IdentityError) as cm:
            fn(*args, **kwargs)
        self.assertEqual(str(cm.exception), code)
        self.assertIn(code, CODES)
        self.assertNotIn(str(self.tmp), repr(cm.exception))
        self.assertIsNone(cm.exception.__cause__)
        return cm.exception

    def names(self, path=None):
        return sorted(p.name for p in (path or self.dir).iterdir())

    def snapshot(self):
        out = {}
        for root, dirs, files in os.walk(self.dir):
            for name in dirs + files:
                p = Path(root) / name
                st = p.lstat()
                out[str(p.relative_to(self.dir))] = (stat.S_IMODE(st.st_mode), st.st_size, p.read_bytes() if p.is_file() and not p.is_symlink() else None)
        return out


class LayoutTests(Case):
    def test_initialize_builds_the_exact_layout_with_private_modes_and_reads_back(self):
        bundle = self.init()
        self.assertEqual(self.names(), [".identity.lock", "current"])  # lock lives OUTSIDE the bundle
        self.assertEqual(self.names(self.dir / "current"), ["consent.key", "identity.json", "tls"])
        self.assertEqual(self.names(self.dir / "current" / "tls"), ["ca.key", "ca.pem", "leaf.key", "leaf.pem"])
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        for rel, mode in ((".", 0o700), ("current", 0o700), ("current/tls", 0o700), (".identity.lock", 0o600),
                          ("current/consent.key", 0o600), ("current/identity.json", 0o600), ("current/tls/ca.key", 0o600),
                          ("current/tls/ca.pem", 0o600), ("current/tls/leaf.key", 0o600), ("current/tls/leaf.pem", 0o600)):
            with self.subTest(rel):
                self.assertEqual(stat.S_IMODE((self.dir / rel).stat().st_mode), mode)
        self.assertEqual(json.loads((self.dir / "current/identity.json").read_text()),
                         {"version": 1, "installation_id": INSTALL, "profile": PROFILE})
        again = self.load()
        self.assertEqual(again.leaf_fingerprint, bundle.leaf_fingerprint)
        self.assertEqual(again.consent_public_key, bundle.consent_public_key)
        self.assertEqual(len(bundle.consent_public_key), 32)
        self.assertEqual(bundle.directory, self.dir / "current")
        self.assertNotIn("PRIVATE", repr(bundle))

    def test_consent_key_file_is_the_existing_consentkey_format(self):
        bundle = self.init()
        self.assertEqual((self.dir / "current/consent.key").stat().st_size, 32)
        self.assertEqual(ConsentKey(self.dir / "current").public_bytes, bundle.consent_public_key)

    def test_second_initialize_is_idempotent_and_never_regenerates(self):
        first = self.init()
        before = self.snapshot()
        second = self.init()
        self.assertEqual(second.leaf_fingerprint, first.leaf_fingerprint)
        self.assertEqual(self.snapshot(), before)

    def test_leaf_is_one_year_at_most_and_passes_a_strict_tls_handshake(self):
        bundle = self.init()
        leaf = x509.load_pem_x509_certificate((self.dir / "current/tls/leaf.pem").read_bytes())
        self.assertLessEqual(leaf.not_valid_after_utc - leaf.not_valid_before_utc, dt.timedelta(days=365, minutes=5))
        self.assertEqual(int(leaf.not_valid_after_utc.timestamp()), bundle.leaf_not_after)
        with self.assertRaises(x509.ExtensionNotFound):
            leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.minimum_version = ssl.TLSVersion.TLSv1_3
        server.load_cert_chain(bundle.leaf_cert_file, bundle.leaf_key_file)
        client = ssl.create_default_context(cafile=str(bundle.ca_file))
        client.verify_flags |= ssl.VERIFY_X509_STRICT
        client.check_hostname = False  # owner leaves are pinned by fingerprint, not by name
        c_in, c_out, s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO()
        c, s = client.wrap_bio(c_in, c_out), server.wrap_bio(s_in, s_out, server_side=True)
        done = [False, False]
        for _ in range(20):
            for idx, (obj, rd, wr) in enumerate(((c, c_in, c_out), (s, s_in, s_out))):
                if not done[idx]:
                    try:
                        obj.do_handshake(); done[idx] = True
                    except ssl.SSLWantReadError:
                        pass
                other = s_in if idx == 0 else c_in
                data = wr.read()
                if data:
                    other.write(data)
            if all(done):
                break
        self.assertTrue(all(done))
        self.assertEqual(ident.leaf_fingerprint(x509.load_der_x509_certificate(c.getpeercert(binary_form=True))),
                         bundle.leaf_fingerprint)

    def test_bundle_feeds_the_pairing_codec(self):
        bundle = self.init()
        catalog = {"installation_id": INSTALL, "protocol_versions": [2]}
        offer = pc.build_offer(bundle.consent_key(), bundle.peer_identity(catalog), nonce=b"\x01" * 32, r_a=b"\x02" * 32,
                               expires_at=1000 + 600, now=1000)
        self.assertEqual(offer.identity.leaf_fingerprint, bundle.leaf_fingerprint)
        self.assertEqual(offer.identity.ca_pem, bundle.ca_pem)

    def test_reader_never_creates_anything(self):
        self.assertCode(ident.E_MISSING, self.load)
        self.assertFalse(self.dir.exists())
        self.dir.mkdir(mode=0o700)
        self.assertCode(ident.E_MISSING, self.load)
        self.assertEqual(self.names(), [])


class CrashTests(Case):
    def record_points(self):
        points, order = [], []
        real_fsync = fs.fsync
        ident._FAULT = points.append
        with patch.object(fs, "fsync", side_effect=lambda fd: (order.append(len(points)), real_fsync(fd))[1]):
            self.init()
        ident._FAULT = None
        shutil.rmtree(self.dir)
        return points

    def test_every_durable_step_is_a_fault_point_and_order_is_write_fsync_rename_parent_fsync(self):
        points = self.record_points()
        for f in ("consent.key", "tls/ca.key", "tls/ca.pem", "tls/leaf.key", "tls/leaf.pem", "identity.json"):
            self.assertIn(f"after_write:{f}", points)
            self.assertLess(points.index(f"after_write:{f}"), points.index(f"after_fsync:{f}"))
        tail = ["after_fsync:tls_dir", "after_fsync:staging_dir", "before_rename", "after_rename", "after_parent_fsync"]
        self.assertEqual(points[-5:], tail)
        self.assertEqual(len(points), 17)

    def test_parent_directory_is_fsynced_after_the_single_rename(self):
        events = []
        real_fsync, real_rename = fs.fsync, os.rename
        with patch.object(fs, "fsync", side_effect=lambda fd: (events.append("fsync"), real_fsync(fd))[1]), \
                patch("os.rename", side_effect=lambda *a, **k: (events.append("rename"), real_rename(*a, **k))[1]):
            self.init()
        self.assertEqual(events.count("rename"), 1)
        self.assertEqual(events[-2:], ["rename", "fsync"])
        self.assertEqual(events.count("fsync"), 6 + 2 + 1)  # 6 files, tls/ + staging dirs, parent

    def test_crash_at_every_point_leaves_no_current_or_a_complete_one_and_recovers(self):
        points = self.record_points()
        for point in points:
            with self.subTest(point=point):
                shutil.rmtree(self.dir, ignore_errors=True)

                def hook(p, target=point):
                    if p == target:
                        raise Crash()
                ident._FAULT = hook
                with self.assertRaises(Crash):
                    self.init()
                ident._FAULT = None
                published = point in ("after_rename", "after_parent_fsync")
                if published:
                    complete = self.load()  # complete bundle, valid as a whole
                    self.assertEqual(self.names(), [".identity.lock", "current"])
                else:
                    self.assertCode(ident.E_MISSING, self.load)  # never a partial `current`
                    self.assertNotIn("current", self.names())
                    self.assertTrue(any(n.startswith(".staging-") for n in self.names()))
                recovered = self.init()
                self.assertEqual(self.names(), [".identity.lock", "current"])  # staging swept, nothing partial
                self.assertEqual(self.load().leaf_fingerprint, recovered.leaf_fingerprint)
                if published:
                    self.assertEqual(recovered.leaf_fingerprint, complete.leaf_fingerprint)  # not regenerated

    def test_ordinary_error_cleans_its_staging_and_publishes_nothing(self):
        def hook(p):
            if p == "after_fsync:tls/leaf.key":
                raise RuntimeError("boom")
        ident._FAULT = hook
        with self.assertRaises(RuntimeError):
            self.init()
        ident._FAULT = None
        self.assertEqual(self.names(), [".identity.lock"])
        self.assertCode(ident.E_MISSING, self.load)

    def test_a_partial_final_bundle_is_refused_never_repaired(self):
        self.init()
        (self.dir / "current/tls/leaf.pem").unlink()
        before = self.snapshot()
        self.assertCode(ident.E_STORAGE, self.load)
        self.assertCode(ident.E_STORAGE, self.init)
        self.assertEqual(self.snapshot(), before)


class ConcurrencyTests(Case):
    CHILD = r'''
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
from invite_rooms import identity as I
count = {"n": 0}
orig = I._new_rsa_key
def counted():
    count["n"] += 1
    return orig()
I._new_rsa_key = counted
while not os.path.exists(sys.argv[3]):
    time.sleep(0.002)
b = I.initialize_identity(sys.argv[2], installation_id="install:" + "a" * 32, profile="default", anchor=os.path.dirname(sys.argv[2]))
print(json.dumps({"fp": b.leaf_fingerprint, "pub": b.consent_public_key.hex(), "keygen": count["n"]}))
'''

    def test_two_concurrent_initializer_processes_publish_exactly_one_bundle(self):
        ident_dir, go = self.tmp / "race", self.tmp / "go"
        procs = [subprocess.Popen([sys.executable, "-B", "-c", self.CHILD, SERVER_DIR, str(ident_dir), str(go)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
        time.sleep(0.5)
        go.write_text("go")
        results = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
            results.append(json.loads(out))
        self.assertEqual(len({r["fp"] for r in results}), 1)
        self.assertEqual(len({r["pub"] for r in results}), 1)
        self.assertEqual(sum(r["keygen"] for r in results), 2)  # one CA key + one leaf key in total: ONE initializer built
        self.assertEqual(sorted(p.name for p in ident_dir.iterdir()), [".identity.lock", "current"])
        self.assertEqual(load_identity(ident_dir, installation_id=INSTALL, profile=PROFILE, anchor=self.tmp).leaf_fingerprint,
                         results[0]["fp"])


class StorageRefusalTests(Case):
    def test_symlinked_identity_dir_is_refused_for_init_and_load(self):
        real = self.tmp / "real"
        real.mkdir(mode=0o700)
        self.dir.symlink_to(real)
        self.assertCode(ident.E_STORAGE, self.init)
        self.assertCode(ident.E_STORAGE, self.load)
        self.assertEqual(list(real.iterdir()), [])

    def test_symlinked_intermediate_component_is_refused(self):
        real = self.tmp / "real"
        real.mkdir(mode=0o700)
        (self.tmp / "hop").symlink_to(real)
        with self.assertRaises(IdentityError) as cm:
            initialize_identity(self.tmp / "hop" / "identity", installation_id=INSTALL, profile=PROFILE, anchor=self.tmp)
        self.assertEqual(str(cm.exception), ident.E_STORAGE)
        self.assertEqual(list(real.iterdir()), [])

    def test_symlinked_lock_is_refused(self):
        self.dir.mkdir(mode=0o700)
        target = self.tmp / "elsewhere"
        target.write_text("x")
        os.chmod(target, 0o600)
        (self.dir / ".identity.lock").symlink_to(target)
        self.assertCode(ident.E_STORAGE, self.init)
        self.assertEqual(target.read_text(), "x")
        self.assertEqual(self.names(), [".identity.lock"])

    def test_symlinked_current_is_refused_and_never_replaced(self):
        self.init()
        elsewhere = self.tmp / "moved"
        os.rename(self.dir / "current", elsewhere)
        (self.dir / "current").symlink_to(elsewhere)
        self.assertCode(ident.E_STORAGE, self.load)
        self.assertCode(ident.E_STORAGE, self.init)
        self.assertTrue((self.dir / "current").is_symlink())
        self.assertEqual(self.names(elsewhere), ["consent.key", "identity.json", "tls"])

    def test_symlinked_members_are_refused(self):
        for member in ("consent.key", "identity.json", "tls/ca.pem", "tls/leaf.key"):
            with self.subTest(member):
                shutil.rmtree(self.dir, ignore_errors=True)
                self.init()
                path = self.dir / "current" / member
                copy = self.tmp / "copy"
                shutil.copy(path, copy)
                os.chmod(copy, 0o600)
                path.unlink()
                path.symlink_to(copy)
                self.assertCode(ident.E_STORAGE, self.load)
        shutil.rmtree(self.dir, ignore_errors=True)
        self.init()
        moved = self.tmp / "tlsdir"
        os.rename(self.dir / "current/tls", moved)
        (self.dir / "current/tls").symlink_to(moved)
        self.assertCode(ident.E_STORAGE, self.load)

    def test_broad_or_wrong_modes_are_refused_not_repaired(self):
        cases = [(".", 0o755), (".", 0o750), ("current", 0o755), ("current/tls", 0o750), (".identity.lock", 0o644),
                 ("current/consent.key", 0o644), ("current/identity.json", 0o640), ("current/tls/leaf.key", 0o660),
                 ("current/tls/ca.key", 0o400), ("current/tls/ca.pem", 0o666)]
        for rel, mode in cases:
            with self.subTest(rel=rel, mode=oct(mode)):
                shutil.rmtree(self.dir, ignore_errors=True)
                self.init()
                target = self.dir / rel
                os.chmod(target, mode)
                self.assertCode(ident.E_STORAGE, self.load)
                self.assertCode(ident.E_STORAGE, self.init)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), mode)  # not repaired
                os.chmod(self.dir, 0o700)

    def test_foreign_uid_is_refused(self):
        self.init()
        with patch("os.getuid", return_value=os.getuid() + 1):
            self.assertCode(ident.E_STORAGE, self.load)
            self.assertCode(ident.E_STORAGE, self.init)
        self.load()

    def test_extra_hardlinked_and_nonregular_members_are_refused(self):
        self.init()
        (self.dir / "current/extra").write_text("x")
        self.assertCode(ident.E_STORAGE, self.load)
        (self.dir / "current/extra").unlink()
        os.link(self.dir / "current/consent.key", self.tmp / "hard")
        self.assertCode(ident.E_STORAGE, self.load)
        os.unlink(self.tmp / "hard")
        (self.dir / "current/identity.json").unlink()
        os.mkfifo(self.dir / "current/identity.json", 0o600)
        self.assertCode(ident.E_STORAGE, self.load)

    def test_foreign_entries_and_odd_staging_in_identity_dir_stop_initialisation(self):
        self.dir.mkdir(mode=0o700)
        (self.dir / "notes.txt").write_text("mine")
        self.assertCode(ident.E_STORAGE, self.init)
        self.assertEqual((self.dir / "notes.txt").read_text(), "mine")
        (self.dir / "notes.txt").unlink()
        (self.dir / ".staging-abc").symlink_to(self.tmp)
        self.assertCode(ident.E_STORAGE, self.init)
        self.assertTrue((self.dir / ".staging-abc").is_symlink())
        self.assertNotIn("current", self.names())

    def test_stale_staging_directories_are_swept_by_the_next_initializer(self):
        self.dir.mkdir(mode=0o700)
        stale = self.dir / ".staging-deadbeef"
        (stale / "tls").mkdir(parents=True, mode=0o700)
        (stale / "consent.key").write_text("half")
        self.init()
        self.assertEqual(self.names(), [".identity.lock", "current"])


class ValidationTests(Case):
    def test_leaf_expired(self):
        issued = time.time() - 400 * 86400
        self.init(now=issued)
        self.assertCode(ident.E_EXPIRED, self.load)
        self.assertEqual(self.load(now=issued + 3600).installation_id, INSTALL)
        self.assertCode(ident.E_EXPIRED, self.init)  # an existing expired bundle is refused, not re-issued
        self.assertTrue((self.dir / "current").exists())

    def test_leaf_validity_over_one_year_is_refused_by_the_reader(self):
        with patch.object(ident, "LEAF_DAYS", 400):
            self.init()
        self.assertCode(ident.E_INVALID, self.load)

    def test_installation_and_profile_must_match_config_and_native(self):
        self.init()
        self.assertCode(ident.E_MISMATCH, self.load, installation_id="install:" + "b" * 32)
        self.assertCode(ident.E_MISMATCH, self.load, profile="other")
        self.assertCode(ident.E_MISMATCH, self.init, installation_id="install:" + "b" * 32)
        self.assertCode(ident.E_MISMATCH, self.init, profile="other")
        for bad in ("", "a/b", "x" * 129, None, 5):
            with self.subTest(repr(bad)):
                self.assertCode(ident.E_INVALID, self.load, installation_id=bad)
        self.assertEqual(self.load().profile, PROFILE)

    def test_tampered_or_mixed_members_are_invalid(self):
        def swap(name, data):
            p = self.dir / "current" / name
            os.chmod(p, 0o600)
            p.write_bytes(data)
            os.chmod(p, 0o600)

        other = tempfile.mkdtemp(dir=self.tmp)
        os.chmod(other, 0o700)
        with patch.object(ident, "_new_rsa_key", _REAL_RSA):  # genuinely different keys than the pool
            donor = initialize_identity(Path(other) / "d", installation_id=INSTALL, profile=PROFILE, anchor=other)
        donor_dir = Path(other) / "d" / "current"
        cases = {
            "leaf_key_from_other_bundle": ("tls/leaf.key", (donor_dir / "tls/leaf.key").read_bytes()),
            "ca_pem_from_other_bundle": ("tls/ca.pem", (donor_dir / "tls/ca.pem").read_bytes()),
            "ca_key_from_other_bundle": ("tls/ca.key", (donor_dir / "tls/ca.key").read_bytes()),
            "leaf_pem_from_other_bundle": ("tls/leaf.pem", (donor_dir / "tls/leaf.pem").read_bytes()),
            "short_consent_seed": ("consent.key", b"\x01" * 31),
            "long_consent_seed": ("consent.key", b"\x01" * 33),
            "pem_trailing_junk": ("tls/leaf.pem", None),
            "garbage_key": ("tls/ca.key", b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"),
            "identity_json_extra_key": ("identity.json", json.dumps({"version": 1, "installation_id": INSTALL, "profile": PROFILE, "x": 1}, sort_keys=True, separators=(",", ":")).encode()),
            "identity_json_version_bool": ("identity.json", json.dumps({"version": True, "installation_id": INSTALL, "profile": PROFILE}, sort_keys=True, separators=(",", ":")).encode()),
            "identity_json_not_canonical": ("identity.json", json.dumps({"version": 1, "installation_id": INSTALL, "profile": PROFILE}, indent=1).encode()),
            "identity_json_garbage": ("identity.json", b"\x00\x01"),
        }
        for label, (name, data) in cases.items():
            with self.subTest(label):
                shutil.rmtree(self.dir, ignore_errors=True)
                self.init()
                if data is None:
                    data = (self.dir / "current" / name).read_bytes() + b"junk"
                swap(name, data)
                self.assertCode(ident.E_INVALID, self.load)
                self.assertCode(ident.E_INVALID, self.init)
        self.assertNotEqual(donor.leaf_fingerprint, "")

    def test_strict_profile_negatives(self):
        now = dt.datetime.now(dt.timezone.utc)
        ca_key, other_key, leaf_key = _POOL
        ca_name = ident._name("mc-rooms-ca")
        ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
        both = [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]

        def ca(days=ident.CA_DAYS):
            return ident._build(subject="mc-rooms-ca", public_key=ca_key.public_key(), issuer=ca_name, issuer_key=ca_key,
                                issuer_ski=ski, days=days, ca=True, now=now)

        def leaf(**kw):
            args = dict(subject="mc-rooms-peer", public_key=leaf_key.public_key(), issuer=ca_name, issuer_key=ca_key,
                        issuer_ski=ski, days=365, ca=False, now=now, eku=both)
            args.update(kw)
            return ident._build(**args)

        ident._validate_pki(ca_key, ca(), leaf_key, leaf(), now)  # control: the profile passes
        bad = {
            "leaf_is_a_ca": leaf(ca=True),
            "server_auth_only": leaf(eku=[ExtendedKeyUsageOID.SERVER_AUTH]),
            "no_eku": leaf(eku=None),
            "signed_by_other_key": leaf(issuer_key=other_key),
            "validity_over_a_year": leaf(days=366),
            "leaf_outlives_ca": leaf(days=365),  # checked below with a short CA
        }
        for label, cert in bad.items():
            with self.subTest(label):
                authority = ca(days=30) if label == "leaf_outlives_ca" else ca()
                with self.assertRaises(IdentityError) as cm:
                    ident._validate_pki(ca_key, authority, leaf_key, cert, now)
                self.assertEqual(str(cm.exception), ident.E_INVALID)
        with self.assertRaises(IdentityError) as cm:  # leaf private key does not belong to the certificate
            ident._validate_pki(ca_key, ca(), other_key, leaf(), now)
        self.assertEqual(str(cm.exception), ident.E_INVALID)
        with self.assertRaises(IdentityError) as cm:
            ident._validate_pki(ca_key, ca(), leaf_key, leaf(), now + dt.timedelta(days=366))
        self.assertEqual(str(cm.exception), ident.E_EXPIRED)
        san = (x509.CertificateBuilder().subject_name(ident._name("x")).issuer_name(ca_name).public_key(leaf_key.public_key())
               .serial_number(1).not_valid_before(now).not_valid_after(now + dt.timedelta(days=10))
               .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski), critical=False)
               .add_extension(x509.ExtendedKeyUsage(both), critical=False)
               .add_extension(x509.SubjectAlternativeName([x509.DNSName("evil.example")]), critical=False)
               .sign(ca_key, hashes.SHA256()))
        with self.assertRaises(IdentityError) as cm:
            ident._validate_pki(ca_key, ca(), leaf_key, san, now)
        self.assertEqual(str(cm.exception), ident.E_INVALID)


if __name__ == "__main__":
    unittest.main()
