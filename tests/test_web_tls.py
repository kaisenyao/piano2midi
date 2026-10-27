"""HTTPS for the iPhone: real certificates made with OpenSSL in a temporary folder; system trust is never touched."""
from pathlib import Path
import shutil
import socket
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch

from piano2midi import web_tls
from piano2midi.web_tls import CA_FILE, prepare_https


def accepts(folder, host):
    """Whether a client that trusts only this certificate authority, as the iPhone will, accepts the server as host."""
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(folder / 'cert.pem', folder / 'key.pem')
    client = ssl.create_default_context(cafile=str(folder / 'ca.pem'))
    near, far = socket.socketpair()
    near.settimeout(5)
    far.settimeout(5)

    def serve():
        try:
            server.wrap_socket(near, server_side=True).close()
        except (ssl.SSLError, OSError):
            near.close()
    thread = threading.Thread(target=serve)
    thread.start()
    try:
        client.wrap_socket(far, server_hostname=host).close()
        return True
    except ssl.SSLCertVerificationError:
        far.close()
        return False
    finally:
        thread.join()


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL unavailable')
class CertificateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / 'phone-tls'

    def prepare(self, name='Test-Mac.local', address='10.0.0.246'):
        with patch.object(web_tls, 'local_name', return_value=name), \
             patch.object(web_tls, 'home_address', return_value=address):
            return prepare_https(self.folder)

    def test_made_once_for_the_iphone_then_a_fresh_certificate_each_start(self):
        self.assertEqual(self.prepare(), ('10.0.0.246', True), 'The page opens at the address')
        self.assertEqual({p.name for p in self.folder.iterdir()},
                         {'ca-key.pem', 'ca.pem', CA_FILE, 'key.pem', 'cert.pem'}, 'Nothing else is left behind')
        self.assertTrue(accepts(self.folder, 'Test-Mac.local'))
        self.assertTrue(accepts(self.folder, '10.0.0.246'), 'The address works too')
        self.assertEqual(self.folder.stat().st_mode & 0o777, 0o700)
        for name in ('ca-key.pem', 'key.pem'):
            self.assertEqual((self.folder / name).stat().st_mode & 0o777, 0o600)
        installed, served = (self.folder / CA_FILE).read_bytes(), (self.folder / 'cert.pem').read_bytes()
        self.assertEqual(self.prepare(address='192.168.1.20'), ('192.168.1.20', False))
        self.assertEqual((self.folder / CA_FILE).read_bytes(), installed, 'The iPhone keeps what it has')
        self.assertNotEqual((self.folder / 'cert.pem').read_bytes(), served)
        self.assertTrue(accepts(self.folder, '192.168.1.20'), 'A new address needs nothing on the phone')

    def test_the_certificate_authority_cannot_vouch_for_a_website(self):
        self.prepare()
        self.prepare(name='www.example.com')  # signed, but outside what the authority may vouch for
        self.assertFalse(accepts(self.folder, 'www.example.com'))

    def test_without_a_home_address_the_name_alone_is_used(self):
        self.assertEqual(self.prepare(address=None), ('Test-Mac.local', True))
        self.assertTrue(accepts(self.folder, 'Test-Mac.local'))


class AddressTests(unittest.TestCase):
    def test_only_home_network_addresses_go_into_the_certificate(self):
        for address, kept in (('10.0.0.246', True), ('192.168.1.20', True), ('100.101.102.103', False)):
            with patch('socket.socket') as sock:
                sock.return_value.__enter__.return_value.getsockname.return_value = (address, 5000)
                self.assertEqual(web_tls.home_address(), address if kept else None)


if __name__ == '__main__':
    unittest.main()
