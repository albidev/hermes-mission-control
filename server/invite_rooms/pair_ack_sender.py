"""P3b-W2b0: the REAL host-A ``pair_ack`` sender over the pinned loopback mTLS client.

``PairingService.send_pair_ack(session_id, send)`` is the canonical activation workflow; this module supplies the
product ``send(endpoint_id, generation, request) -> reply`` for it.  Every trust and destination input is re-read at
SEND time from owner state, never from the caller:

* the peer record (local role ``host``, state ``confirmed``/``acknowledged``, exact generation, CA PEM, leaf pin,
  binding) comes from ONE non-waiting shared journal read (``JournalLinkAuthority.peer_trust``) whose lock is released
  before any identity-file, socket or TLS work;
* the local certificate/key come from the verified local identity bundle;
* the destination is ONLY the owner-private ``pairing.control_endpoints`` entry for that journal peer (literal
  loopback; there is no default, DNS, fallback or request/invitation/registry-sourced endpoint);
* the peer CA PEM is handed to ``ssl`` as in-memory ``cadata`` (never written to disk).

The connector is ``PairAckConnector``: operation-fixed to ``pair_ack`` (a confirmed link may send nothing else) with the
durable ``JournalLinkAuthority.admit`` decision immediately before the first frame.  Any failure (busy/stale/corrupt/
missing state, missing endpoint, TLS/pin failure, peer unavailable, bad reply) raises the generic
``PairAckUnavailable`` BEFORE or without any effect on the durable ``confirmed`` state.  Nothing here starts a listener.
"""
from __future__ import annotations

from typing import Callable, Mapping, Optional

from invite_rooms import identity as identity_module
from invite_rooms.control.connector import LinkBinding, LinkIdentity, make_pair_ack_connector
from invite_rooms.pairing_journal import Journal, JournalError, JournalLinkAuthority
from invite_rooms.runtime_joining import PinnedControlChannel


class PairAckUnavailable(RuntimeError):
    """Generic, text-free: the peer could not be reached/verified; the durable local state is untouched."""

    def __init__(self):
        super().__init__("pair_ack unavailable")


def pair_ack_sender(config, *, timeout: float = 10.0, relay_manager=None) -> Optional[Callable[[str, int, dict], dict]]:
    """Config-scoped ``send(endpoint_id, generation, request)`` over pinned loopback or relay transport."""
    pairing = getattr(config, "pairing", None)
    endpoints = getattr(pairing, "control_endpoints", None)
    relay_links = getattr(pairing, "relay_links", None)
    if pairing is None or (not endpoints and not relay_links):
        return None

    def send(endpoint_id, generation, request) -> dict:
        try:
            if not isinstance(endpoint_id, str) or type(generation) is not int or not isinstance(request, dict):
                raise PairAckUnavailable()
            journal = Journal(pairing.journal_dir, anchor=pairing.journal_anchor)
            authority = JournalLinkAuthority(journal)
            # strict variant: absent/stale/wrong-role trust is None, but a busy/corrupt LOCAL journal keeps its typed
            # JournalError (re-raised below) so the route maps it to the established 503/404, never to "peer down".
            trust = authority.peer_trust_strict(endpoint_id, generation, role="host",
                                                states=("confirmed", "acknowledged"))
            if trust is None:
                raise PairAckUnavailable()
            direct = endpoints.get(trust.peer_id) if isinstance(endpoints, Mapping) else None
            stream_client = None
            relay_channel = None
            if direct is not None:
                address = tuple(direct)
                transport = "loopback"
            else:
                link = relay_links.get(endpoint_id) if isinstance(relay_links, Mapping) else None
                if (relay_manager is None or link is None or link.generation != generation
                        or link.peer_id != trust.peer_id):
                    raise PairAckUnavailable()
                address = None
                transport = "relay"
                stream_client = relay_manager.client_for(trust)
                relay_channel = "control"
            ca_pem = trust.ca_pem.decode("ascii") if isinstance(trust.ca_pem, bytes) else trust.ca_pem
            bundle = identity_module.load_identity(pairing.identity_dir, installation_id=config.installation_id,
                                                   profile=config.profile)
            binding = LinkBinding(endpoint_id=endpoint_id, generation=generation, address=address,
                                  peer_ca_file=None, peer_ca_data=ca_pem, peer_fingerprint=trust.fingerprint,
                                  expected_binding=trust.binding_dict(), timeout=float(timeout), transport=transport)
            identity = LinkIdentity(cert_file=str(bundle.leaf_cert_file), key_file=str(bundle.leaf_key_file))
            connector = (make_pair_ack_connector(binding, identity, authority)
                        if stream_client is None else make_pair_ack_connector(binding, identity, authority,
                            stream_client=stream_client, relay_channel=relay_channel))
            response = connector.forward_pair_ack(request)
            # Same strict decoder the W1 joining client uses (200 + unique-key JSON object, bounded).
            return PinnedControlChannel._decode(response)
        except (PairAckUnavailable, JournalError):
            raise
        except Exception:
            raise PairAckUnavailable() from None
    return send
