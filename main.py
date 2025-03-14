from interlayer import ProxyInterlayer
from dns_server import DNSWrapper
from shared_storage import SharedStorage

def main():
    # init shared storage
    SharedStorage.init()

    # SOCKS5 Proxy Configuration
    upstream_host = "213.18.207.142"
    upstream_port = 5337

    # Authentication Credentials
    username = "2Bk2fQo4E"
    password = "7CWp9vTi"

    # local proxy configuration
    listen_host = "0.0.0.0"
    listen_port = 1280

    # DNS configuration
    dns_host = "0.0.0.0"
    dns_port = 53

    # initializing of interlayer runs new thread
    ProxyInterlayer(upstream_host, upstream_port, username, password, listen_host, listen_port)
    DNSWrapper(dns_host, dns_port)
    print('Hello, world!')


if __name__ == "__main__":
    main()
