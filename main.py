import time
from interlayer import ProxyInterlayer
from dns_server import DNSWrapper
from shared_storage import SharedStorage
from custom_types import MessageType, global_log

def main():
    # init shared storage
    SharedStorage.init()

    # SOCKS5 Proxy Configuration
    upstream_host = "213.18.207.142"
    upstream_port = 5337

    # local proxy configuration
    listen_host = "0.0.0.0"
    listen_port = 1280

    # DNS configuration
    dns_host = "0.0.0.0"
    dns_port = 1053

    # initializing of interlayer runs new thread
    proxy = ProxyInterlayer(upstream_host, upstream_port, listen_host, listen_port)
    dns = DNSWrapper(dns_host, dns_port)

    #global_log("NOTICE: both servers will be stopped in 10 seconds", MessageType.INFO)
    #time.sleep(10)
    #global_log("Stopping servers...", MessageType.INFO)
    #proxy.stop_server()
    #dns.stop_server()


if __name__ == "__main__":
    main()
