"""HTTPS on the home network, which Safari requires before a page may use the microphone.

A certificate authority is made once for this Mac and installed on the iPhone by hand; it can vouch only for .local
names and home network addresses. Every start signs a fresh certificate for the Mac's current address and its .local
name, so the passing of time never needs anything on the iPhone. The page is opened at the address: Safari does not
keep the microphone permission for a .local name, and would ask at every visit.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile

CA_FILE = 'piano2midi-CA.cer'  # the one file for the iPhone; it holds no secret
HOME_NETWORKS = [ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
CA_CONFIG = '''[req]
prompt = no
distinguished_name = name
x509_extensions = ca
[name]
CN = piano2midi on {mac}
[ca]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
nameConstraints = critical,permitted;DNS:local,{networks}
subjectKeyIdentifier = hash
'''
SERVER_CONFIG = '''[req]
prompt = no
distinguished_name = name
[name]
CN = {name}
[server]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = {names}
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
'''


def local_name() -> str:
    """The Mac's Bonjour name, for example My-Mac.local: it stays when the address changes."""
    try:
        name = subprocess.run(['scutil', '--get', 'LocalHostName'], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ''
    return f'{name or socket.gethostname().split(".")[0]}.local'


def home_address() -> str | None:
    """The Mac's address on the home network, when the certificate authority may vouch for it."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(('192.0.2.1', 9))  # sends nothing; only picks the network the Mac would use
            address = probe.getsockname()[0]
        except OSError:
            return None
    return address if any(ipaddress.ip_address(address) in n for n in HOME_NETWORKS) else None


def prepare_https(folder: Path) -> tuple[str, bool]:
    """Make the certificate authority if it is missing, and a fresh certificate for this start.

    Returns where the iPhone opens the page (the Mac's home network address, or its .local name without one), and
    whether the certificate authority is new: then the iPhone must install it.
    """
    openssl = shutil.which('openssl')
    if not openssl:
        raise RuntimeError('OpenSSL is required for HTTPS')

    def run(*args):
        subprocess.run([openssl, *map(str, args)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       timeout=60)

    name, address = local_name(), home_address()
    new = not all((folder / f).is_file() for f in ('ca-key.pem', 'ca.pem', CA_FILE))
    previous = os.umask(0o077)  # private keys are never readable by others, not even for a moment
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            if new:
                networks = ','.join(f'permitted;IP:{n.network_address}/{n.netmask}' for n in HOME_NETWORKS)
                (config / 'ca.cnf').write_text(CA_CONFIG.format(mac=name.removesuffix('.local'), networks=networks))
                run('req', '-new', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-days', '3650',
                    '-config', config / 'ca.cnf', '-keyout', folder / 'ca-key.pem', '-out', folder / 'ca.pem')
                run('x509', '-in', folder / 'ca.pem', '-outform', 'DER', '-out', folder / CA_FILE)
            names = f'DNS:{name}' + (f',IP:{address}' if address else '')
            (config / 'server.cnf').write_text(SERVER_CONFIG.format(name=name, names=names))
            run('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-config', config / 'server.cnf',
                '-keyout', folder / 'key.pem', '-out', config / 'server.csr')
            run('x509', '-req', '-in', config / 'server.csr', '-CA', folder / 'ca.pem', '-CAkey', folder / 'ca-key.pem',
                '-set_serial', secrets.randbits(63), '-days', '397', '-sha256',
                '-extfile', config / 'server.cnf', '-extensions', 'server', '-out', folder / 'cert.pem')
    finally:
        os.umask(previous)
    return address or name, new
