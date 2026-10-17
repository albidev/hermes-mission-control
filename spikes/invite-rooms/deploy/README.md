# F1 cross-host kit

Goal: prove the native Room flow between **two Hermes installs on distinct
networks**, with both installs dialing **outbound only** to a relay on the VPS,
and the relay seeing ciphertext only. Same code path already green on one Mac
(`gate_f1_local.py`, run d2758ec3: real model turn `LAB_READY`, duplicate
dispatch → `replayed=true`, 102 relay DATA frames, zero plaintext markers).

```
Host A (Mac, home network)          VPS (public :443 only)           Host B (other network, inbound closed)
hermes serve (isolated)             Caddy :443 ─TLS─► relay_daemon   hermes gateway (isolated, api_server on loopback)
 └ connector host ──WSS outbound──►        127.0.0.1:8443 ◄──WSS outbound── connector target
```

## 0. What you need
- VPS: Debian/Ubuntu, SSH as a sudo user, a DNS name (`relay.example.com`) with A/AAAA → VPS.
- Host B: any machine on a *different* network (laptop on phone hotspot, or 2nd small VPS) with
  Python ≥3.11, `websockets>=13`, Hermes installed. No port forwarding, no inbound rules.
- This directory + `../{relay.py,relay_daemon.py,connector.py,tls_stream.py,room_firewall.py,adapter.py,pki.py}`.

## 1. VPS (relay)
```bash
sudo bash vps-bootstrap.sh relay.example.com     # user, venv, firewall 22+443, Caddy, unit (not started)
# provision the link (prints one credential file per endpoint; route file keeps hashes only)
sudo -u rooms-relay /opt/rooms-relay/venv/bin/python /opt/rooms-relay/relay_daemon.py provision \
  --routes /etc/rooms-relay/routes.json --out-dir /etc/rooms-relay/creds \
  --link-id f1-link --room-id <ROOM_ID> --source <HOST_A_INSTALL_ID> --target <HOST_B_INSTALL_ID>
sudo systemctl enable --now rooms-relay caddy
```
Room id and install ids come from the native invitation on Host B (step 3) — provision after it.
Copy `creds/f1-link.<id>.credential` to the matching host over SSH (`scp`, mode 0600), then
**delete both credential files from the VPS**: the relay only needs `routes.json` (digests).

## 2. Per-owner inner-TLS identities (on each host, never on the VPS)
```bash
python pki.py init --out ~/.rooms-f1/pki --name host-a   # on A
python pki.py init --out ~/.rooms-f1/pki --name host-b   # on B
```
Exchange **only** `ca.pem` + `fingerprint` (public). Private keys never move.

## 3. Native invitation (Host B owner consent)
Host B owner runs the native invitation on their own gateway (loopback API) and gives Host A the
grant + catalog out of band. Nothing about the invitation crosses the relay before both connectors
are configured; opening a link never creates a grant.

## 4. Connectors
Fill `host-a.json` / `host-b.json` from the templates, then:
```bash
python connector.py target --config host-b.json   # on B → {"event":"target_ready"}
python connector.py host   --config host-a.json   # on A → {"event":"host_ready","adapter_url":...}
```
Register the peer on Host A with `target_url = adapter_url` (as in gate C).

## 5. Run + evidence
Run the `@peer` turn on Host A, then on **each host**:
```bash
bash collect-evidence.sh A|B relay.example.com > evidence-<A|B>-$(date +%s).json
```
and on the VPS `sudo bash collect-evidence.sh relay relay.example.com`.

Pass criteria (all required for F1):
1. Distinct public egress IPs for A and B (`egress_ip`), B has **zero** non-loopback listeners.
2. A's only listener is the loopback adapter; VPS exposes only 22/443 (`ufw status`, `ss -ltn`).
3. Remote reply in A's canonical log == B's run output; duplicate dispatch → one durable receipt, `replayed=true`.
4. Relay journal shows routes/counters only — no grant, prompt or reply text (`grep` checks in the script).
5. Relay restart mid-idle: connectors reconnect with incarnation+1, no replayed POST on B.
6. Stop/revoke on A behaves as in H (native semantics), unaffected by the relay hop.

## Route reload policy (MVP)
SIGHUP can atomically **add** routes. Removing a route or changing its scope/endpoints/
credential digests is explicitly rejected with `route_reload_rejected`; no partial table
is applied and no revocation is claimed. Stop/restart the relay with the replacement
route file to remove routes or rotate S2. This disconnects active transport sessions;
it is separate from revoking the native S4 grant or stopping an admitted run.

## Teardown
`systemctl disable --now rooms-relay`, remove `/etc/rooms-relay`, revoke the native grant on B,
delete `~/.rooms-f1` on both hosts.
