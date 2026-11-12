"""Independent transport probes. Fixture only, no native identity/admission."""
import sys,ssl,time,json,unittest
from pathlib import Path
from unittest.mock import patch
W=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(W/'spikes/invite-rooms'))
import connector as c
from room_firewall import ForwardedRequest
from gate_f1_local import NetworkTransport

class ReplayPolicy(unittest.TestCase):
    def test_real_mtls_missing_client_certificate_is_not_retried(self):
        t=NetworkTransport(); t.start()
        try:
            # Real target demands a certificate. Use real pinned CA but deliberately
            # omit the client certificate, provoking a TLS certificate-required alert.
            from room_firewall import RoomFirewall
            sys.path.insert(0,str(W/'spikes/invite-rooms/tests'))
            from test_room_firewall import SCOPE
            t.native_receipt_db=t.root/'unused-fixture.db'
            fw=RoomFirewall(expected_scope=SCOPE,backend=lambda _:None,verify_native=lambda *_:False)
            t.bind('auth-probe',fw,target_url='http://127.0.0.1:1',scope=SCOPE)
            inner=t.host_cfg['inner_tls']
            hop=c.HostConnector(t.host_client,inner=inner)
            ctx=ssl.create_default_context(cafile=inner['peer_ca_file']); ctx.check_hostname=False
            ctx.minimum_version=ctx.maximum_version=ssl.TLSVersion.TLSv1_3
            hop.context=ctx
            with patch.object(c,'HOLD_BOUND',.5),patch.object(t.host_client,'open_stream',wraps=t.host_client.open_stream) as opened:
                errors=[]
                original=c.wrap_client
                def wrap(*a,**kw):
                    try: return original(*a,**kw)
                    except ssl.SSLError as exc:
                        errors.append(getattr(exc,'reason',None)); raise
                read_errors=[]
                receive=c.recv_message
                def observe_receive(*a,**kw):
                    try: return receive(*a,**kw)
                    except OSError as exc:
                        read_errors.append(dict(type=type(exc).__name__,reason=getattr(exc,'reason',None))); raise
                with patch.object(c,'wrap_client',side_effect=wrap),patch.object(c,'recv_message',side_effect=observe_receive):
                    with self.assertRaises(ssl.SSLError):
                        hop.forward(ForwardedRequest('GET','/v1/room-members/capabilities',(),b''))
                print('REAL_MTLS_NO_CLIENT_CERT='+json.dumps(dict(attempts=opened.call_count,fail_closed=True,handshake_errors=errors,read_errors=read_errors)))
                self.assertEqual(opened.call_count,1,'real TLS client-certificate rejection was retried')
        finally: t.close()

class CallPolicy(unittest.TestCase):
    def hop(self):
        class Sock:
            def settimeout(self,*a): pass
            def close(self): pass
        class Client:
            def open_stream(self,**kw): return Sock()
        h=object.__new__(c.HostConnector); h.client=Client(); h.context=None; h.peer_fingerprint='0'*64
        return h
    def test_fatal_tls_auth_alert_must_not_retry(self):
        h=self.hop()
        exc=ssl.SSLError(1,'[SSL: TLSV13_ALERT_CERTIFICATE_REQUIRED] tlsv13 alert certificate required')
        with patch.object(c,'HOLD_BOUND',.23), patch.object(c,'wrap_client',side_effect=exc) as wrap:
            try: h.forward(ForwardedRequest('GET','/v1/runs/fixture',(),b''))
            except ssl.SSLError: pass
            print('FATAL_TLS_AUTH_ALERT_ATTEMPTS='+str(wrap.call_count))
            self.assertEqual(wrap.call_count,1,'fatal peer authentication alert was retried')
    def test_local_pin_error_and_certverification_error_are_not_retried(self):
        for exc in (c.TLSIdentityError('fixture pin mismatch'),ssl.SSLCertVerificationError(1,'fixture certificate')):
            with patch.object(c,'wrap_client',side_effect=exc) as wrap:
                with self.assertRaises(ssl.SSLError): self.hop().forward(ForwardedRequest('GET','/v1/runs/fixture',(),b''))
                self.assertEqual(wrap.call_count,1)
    def test_other_posts_are_not_resent_after_stream_open(self):
        for path in ('/v1/room-members/grants/refresh','/v1/room-members/grants/revoke','/v1/runs/fixture/stop','/v1/runs/fixture/resolve-unknown','/v1/runs'):
            with patch.object(c,'wrap_client',side_effect=OSError('fixture disconnect')) as wrap:
                with self.assertRaises(OSError): self.hop().forward(ForwardedRequest('POST',path,(),b'{}'))
                self.assertEqual(wrap.call_count,1)

if __name__=='__main__': unittest.main(verbosity=2)
