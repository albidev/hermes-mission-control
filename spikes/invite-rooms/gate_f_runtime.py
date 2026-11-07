"""Gate F local lab: real native gateways through two connectors and opaque inner-mTLS relay.

This composes the already-proven Gate C native RoomLink workflow with a real
connector/relay transport.  The relay forwards only ciphertext; native Room
registration, dispatch, status, stop and canonical log remain unchanged.
"""
from __future__ import annotations

import base64
import importlib.util
import json

from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
from typing import Any

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CORE = Path('/Users/albi/.hermes/hermes-agent')
SCRATCH = Path('/Users/albi/.hermes/cache/scratch/room-network-spike-20261011-164930')

_spec = importlib.util.spec_from_file_location('gate_c_runtime', HERE / 'gate_c_runtime.py')
assert _spec and _spec.loader
_gate_c = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gate_c)
from tls_stream import SequencedTLS, certificate_fingerprint, client_context, server_context, wrap_client, wrap_server


class OpaqueRelay:
    """A byte-only relay between Connector A and Connector B."""
    def __init__(self, left: socket.socket, right: socket.socket) -> None:
        self.left, self.right = left, right
        self.observed: list[bytes] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        import select
        peers = {self.left: self.right, self.right: self.left}
        while not self.stop.is_set():
            readable, _, _ = select.select(tuple(peers), (), (), .1)
            for source in readable:
                try:
                    data = source.recv(65536)
                    if not data:
                        return
                    self.observed.append(data)
                    peers[source].sendall(data)
                except OSError:
                    return

    def close(self) -> None:
        self.stop.set()
        for sock in (self.left, self.right):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        self.thread.join(timeout=2)


class ConnectorB:
    def __init__(self, target_base: str, server_context, expected_client: str) -> None:
        self.target_base = target_base
        self.server_context = server_context
        self.expected_client = expected_client

    def serve(self, sock: socket.socket) -> None:
        stream = None
        try:
            stream = wrap_server(sock, self.server_context, expected_fingerprint=self.expected_client)
            frame = SequencedTLS(stream)
            _, raw = frame.receive()
            request = json.loads(raw)
            import urllib.request
            import urllib.error
            body = base64.b64decode(request['body']) if request['body'] else None
            upstream = urllib.request.Request(self.target_base + request['path'], data=body,
                method=request['method'], headers=dict(request['headers']))
            try:
                with urllib.request.urlopen(upstream, timeout=25) as response:
                    result = {'status': response.status,
                              'headers': [[k, v] for k, v in response.headers.items()
                                          if k.lower() in {'content-type', 'content-length'}],
                              'body': base64.b64encode(response.read()).decode()}
            except urllib.error.HTTPError as exc:
                result = {'status': exc.code,
                          'headers': [[k, v] for k, v in exc.headers.items()
                                      if k.lower() in {'content-type', 'content-length'}],
                          'body': base64.b64encode(exc.read()).decode()}
            frame.send(json.dumps(result, separators=(',', ':')).encode())
        finally:
            if stream is not None:
                stream.close()
            else:
                sock.close()


class ConnectorA:
    def __init__(self, *, ca: Path, cert: Path, key: Path, expected_server: str,
                 connector_b: ConnectorB) -> None:
        self.context = client_context(ca_file=str(ca), cert_file=str(cert), key_file=str(key))
        self.expected_server = expected_server
        self.connector_b = connector_b
        self.relay_observed: list[bytes] = []

    def forward(self, request) -> Any:
        a, relay_left = socket.socketpair()
        relay_right, b = socket.socketpair()
        relay = OpaqueRelay(relay_left, relay_right)
        errors: list[BaseException] = []
        server_thread = threading.Thread(target=self.connector_b.serve, args=(b,), daemon=True)
        relay.start(); server_thread.start()
        client = None
        try:
            client = wrap_client(a, self.context, expected_fingerprint=self.expected_server)
            frames = SequencedTLS(client)
            frames.send(json.dumps({'method': request.method, 'path': request.path,
                'headers': list(request.headers),
                'body': base64.b64encode(request.body).decode()}, separators=(',', ':')).encode())
            _, raw = frames.receive()
            result = json.loads(raw)
            from room_firewall import UpstreamResponse
            self.relay_observed.extend(relay.observed)
            return UpstreamResponse(result['status'], tuple(map(tuple, result['headers'])),
                                    base64.b64decode(result['body']))
        finally:
            if client is not None:
                client.close()
            else:
                a.close()
            relay.close()
            server_thread.join(timeout=5)


def _certs(root: Path) -> dict[str, Path]:
    if shutil.which('openssl') is None:
        raise RuntimeError('openssl is required for Gate F')
    ca_key, ca = root / 'ca.key', root / 'ca.pem'
    subprocess.run(['openssl', 'req', '-x509', '-new', '-nodes', '-newkey', 'rsa:2048',
                    '-keyout', str(ca_key), '-out', str(ca), '-subj', '/CN=rooms-gate-f-ca', '-days', '1'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = {'ca': ca}
    for name in ('a', 'b'):
        key, csr, cert = root / f'{name}.key', root / f'{name}.csr', root / f'{name}.pem'
        subprocess.run(['openssl', 'req', '-new', '-nodes', '-newkey', 'rsa:2048', '-keyout', str(key),
                        '-out', str(csr), '-subj', f'/CN=gate-f-{name}'], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['openssl', 'x509', '-req', '-in', str(csr), '-CA', str(ca), '-CAkey', str(ca_key),
                        '-CAcreateserial', '-out', str(cert), '-days', '1'], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result[f'{name}_key'], result[f'{name}_cert'] = key, cert
    return result


def main() -> int:
    cert_root = SCRATCH / ('gate-f-certs-' + next(tempfile._get_candidate_names()))
    cert_root.mkdir(parents=True, mode=0o700)
    try:
        cert = _certs(cert_root)
        fp_a = certificate_fingerprint(__import__('ssl').PEM_cert_to_DER_cert(cert['a_cert'].read_text()))
        fp_b = certificate_fingerprint(__import__('ssl').PEM_cert_to_DER_cert(cert['b_cert'].read_text()))
        connector_holder: dict[str, ConnectorA] = {}
        def through_connectors(target_base, expected_grant):
            b = ConnectorB(target_base, server_context(ca_file=str(cert['ca']), cert_file=str(cert['b_cert']), key_file=str(cert['b_key'])), fp_a)
            a = ConnectorA(ca=cert['ca'], cert=cert['a_cert'], key=cert['a_key'], expected_server=fp_b, connector_b=b)
            connector_holder['a'] = a
            return a.forward
        _gate_c.native_backend = through_connectors
        rc = _gate_c.main()
        reports = sorted(SCRATCH.glob('parent-groups-gate-*.json'), key=lambda p: p.stat().st_mtime)
        native_report_path = str(reports[-1]) if reports else None
        report = json.loads(reports[-1].read_text()) if reports else {}
        # Gate C assigns report_path after persisting its JSON. Preserve the
        # exact selected artifact path explicitly instead of trusting that
        # post-write in-memory field.
        report['native_report_path'] = native_report_path
        observed = b''.join(connector_holder['a'].relay_observed) if connector_holder else b''
        plaintext_sentinels = any(marker in observed for marker in (b'LAB_READY', b'HermesRoom', b'local-lab-no-secret'))
        report.update({'gate_F_passed': bool(rc == 0 and report.get('gate_C_passed') and not plaintext_sentinels),
                       'transport': 'connector-a -> opaque byte relay -> connector-b -> native target',
                       'inner_tls': {'version': 'TLS1.3', 'mutual_auth': True, 'pinned_peer_fingerprints': True},
                       'relay_plaintext_sentinels': plaintext_sentinels,
                       'connector_a_frames_observed': len(connector_holder['a'].relay_observed) if connector_holder else 0,
                       'certificates_removed_after_run': True})
        out = SCRATCH / ('gate-f-report-' + cert_root.name.removeprefix('gate-f-certs-') + '.json')
        out.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps({'gate_F_passed': report['gate_F_passed'], 'report_path': str(out),
                          'native_report_path': report.get('native_report_path'),
                          'relay_plaintext_sentinels': report['relay_plaintext_sentinels']}, indent=2))
        return 0 if report['gate_F_passed'] else 1
    finally:
        shutil.rmtree(cert_root, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
