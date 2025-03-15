from __future__ import annotations
import asyncio
import socket
from typing import TYPE_CHECKING
from custom_types import HostData, UpstreamProxy, MessageType, global_log
from shared_storage import SharedStorage

if TYPE_CHECKING:
    from interlayer import ProxyInterlayer



class InterlayerInstance:
    def __init__(self, parent: ProxyInterlayer):
        self.parent = parent
        parent.instances += 1
        self.instance = parent.instances
        self.upstream = None
        self.closed_by_client = False


    def log(self, msg, msg_type=MessageType.DEBUG):
        global_log(f"[PROXY:{self.parent.listen_port}:{self.instance}] {msg}", msg_type)


    # converts domain name to IP address and back if needed
    # IMPORTANT: we can't simply replace one host with other, because HTTP request
    # contains original host (in domain form in most cases) in 'Host' header, and such
    # request will lead to error (if redirect one host to another for example).
    # Replaces will work only in combination with our "real" DNS server, which will
    # act as in proxychains and return virtual IPs (225.X.X.X) for any domain
    async def pseudo_dns(self):
        ipv4_req = b"\x05\x01\x00\x01"
        host_req = b"\x05\x01\x00\x03"
        self.log(f"DEBUG: running pseudo DNS on {self.host_data.target_host}:{self.host_data.dst_port}")

        try:
            if self.host_data.target_host.startswith('223.'):
                real_host = await SharedStorage.ip_to_host(socket.inet_aton(self.host_data.target_host))
                self.host_data.target_host = real_host.decode()
                self.host_data.type = 3
        except Exception as e:
            self.log(f'Error somewhere in shared memory: {e}', MessageType.ERROR)

        # build SOCKS request
        if self.host_data.type == 1:
            self.host_data.to_connect = ipv4_req + \
            socket.inet_aton(self.host_data.target_host) + self.host_data.dst_port.to_bytes(2, "big")
        elif self.host_data.type == 3:
            self.host_data.to_connect = host_req + bytes([len(self.host_data.target_host)]) +\
                self.host_data.target_host.encode() + self.host_data.dst_port.to_bytes(2, "big")


    async def forward(self, src: asyncio.StreamReader, dst: asyncio.StreamWriter, label: str):
        # tune up for max performance on your machine
        limit = 8192
        default_timeout = 0.004  # 4 ms

        try:
            while True:
                data = await asyncio.wait_for(src.read(limit), default_timeout)
                if not data:
                    self.log(f'No data left {label}')
                    break

                self.log(f'Writing data {label}')
                dst.write(data)
                await dst.drain()

        except asyncio.TimeoutError:
            pass
        # HOOK: if direction is from proxy and error is 'Connection reset by peer'
        # it means client probably closed connection, so we can close connection too
        except ConnectionResetError:
            if label == 'from_proxy':
                self.log(f'Connection reset by client', MessageType.INFO)
                self.closed_by_client = True
        except Exception as e:
            self.log(f"Error on forwarding: {e}", MessageType.ERROR)


    async def close_writer(self, writer):
        try:
            writer.close()
            await writer.wait_closed()
        except Exception: # connection already dropped
            pass


    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.log("New connection")

        try:
            # here - connect request base, always 4 bytes
            self.request = await self.client_auth(reader, writer)
            if not (self.request and isinstance(self.request, bytes) and len(self.request) == 4):
                return

            # get host data from CONNECT request
            self.host_data = await self.handle_host_data(reader, writer)
            if not self.host_data:
                return

            # HOOK: let 'pseudo_dns' to build 'to_connect' byte string
            await self.pseudo_dns()

            # Open connection to real SOCKS5 proxy
            self.upstream = await self.upstream_auth(writer)
            if not self.upstream:
                return

            # Start bidirectional proxying
            self.log("Proxying data")
            while True:
                # 1. response is always after request, so we can wait for request first
                # 2. if local reader is EOF, connection is closed
                if reader.at_eof() or self.upstream.reader.at_eof() or self.closed_by_client:
                    self.log('Connection closed')
                    break
                await self.forward(reader, self.upstream.writer, 'to_proxy')
                await self.forward(self.upstream.reader, writer, 'from_proxy')

        except Exception as e:
            self.log(f"Error in handler: {e}", MessageType.ERROR)
        finally:
            await self.close_writer(writer)
            if self.upstream:
                await self.close_writer(self.upstream.writer)


    async def handle_host_data(self, reader, writer) -> [HostData, None]:
        addr_type = self.request[3]
        host_data = HostData(addr_type)

        if addr_type == 1:  # IPv4
            self.log("Reading IPv4 address")
            dst_ip = socket.inet_ntoa(await reader.readexactly(4))
            host_data.dst_port = int.from_bytes(await reader.readexactly(2), "big")
            host_data.target_host = dst_ip

        elif addr_type == 3:  # Domain name
            self.log("Reading domain name")
            domain_len = await reader.readexactly(1)
            host_data.target_host = (await reader.readexactly(domain_len[0])).decode()
            host_data.dst_port = int.from_bytes(await reader.readexactly(2), "big")

        else:
            self.log("Unsupported target type", MessageType.ERROR)
            writer.write(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            await self.close_writer(writer)
            return None

        self.log(f"Intercepted request: {host_data.target_host}:{host_data.dst_port}")
        return host_data


    async def upstream_auth(self, writer) -> [UpstreamProxy, None]:
        self.log("Connecting to upstream proxy")
        upstream = UpstreamProxy()
        upstream.reader, upstream.writer = \
            await asyncio.open_connection(self.parent.upstream_host, self.parent.upstream_port)

        # Forward authentication to upstream proxy
        self.log("Authenticating at upstream proxy")
        upstream.writer.write(b"\x05\x01\x02")
        await upstream.writer.drain()

        auth_response = await upstream.reader.readexactly(2)
        if auth_response[1] != 2:
            self.log("Upstream proxy does not support authentication", MessageType.ERROR)
            await self.close_writer(writer)
            await self.close_writer(upstream.writer)
            return None

        # Send username/password authentication to upstream proxy
        self.log("Sending credentials to upstream proxy")
        auth_packet = (
                b"\x01" + bytes([len(self.parent.username)]) + self.parent.username.encode() +
                bytes([len(self.parent.password)]) + self.parent.password.encode()
        )
        upstream.writer.write(auth_packet)

        await upstream.writer.drain()
        auth_result = await upstream.reader.readexactly(2)
        if auth_result[1] != 0:
            self.log("Authentication failed at upstream proxy", MessageType.ERROR)
            await self.close_writer(writer)
            await self.close_writer(upstream.writer)
            return None
        self.log("Authentication successful at upstream proxy")

        # Send connection request to upstream proxy
        self.log(f"Connecting to target host {self.host_data.target_host}:{self.host_data.dst_port}")
        upstream.writer.write(self.host_data.to_connect)
        await upstream.writer.drain()

        upstream_response = await upstream.reader.readexactly(10)
        if upstream_response[1] != 0:
            self.log(f"Upstream proxy failed to connect to {self.host_data.target_host}:{self.host_data.dst_port}")
            await self.close_writer(writer)
            await self.close_writer(upstream.writer)
            return None
        self.log(f"Connected to {self.host_data.target_host}:{self.host_data.dst_port}")

        # Send successful response back to client
        writer.write(upstream_response)
        await writer.drain()
        return upstream


    async def client_auth(self, reader, writer) -> [bytes, None]:
        # Read greeting (version + auth methods)
        self.log("Reading greeting")
        greeting = await reader.readexactly(2)
        if greeting[0] != 5:  # Ensure it's SOCKS5
            self.log("Invalid SOCKS version", MessageType.ERROR)
            await self.close_writer(writer)
            return None
        self.log("Valid SOCKS version 5")

        self.log("Reading auth methods")
        num_methods = greeting[1]
        methods = await reader.readexactly(num_methods)
        if 2 not in methods:  # Check if username/password auth is supported
            self.log(f"No acceptable auth methods: {methods}", MessageType.ERROR)
            writer.write(b"\x05\xFF")  # No acceptable methods
            await writer.drain()
            await self.close_writer(writer)
            return None
        self.log("Auth methods accepted")

        # Respond with authentication method (username/password)
        self.log("Authenticating")
        writer.write(b"\x05\x02")
        await writer.drain()
        # Read username/password authentication request
        auth_header = await reader.readexactly(1)
        if auth_header[0] != 1:  # Ensure it's username/password auth
            self.log("Invalid authentication version", MessageType.ERROR)
            await self.close_writer(writer)
            return None
        self.log("Valid authentication version 1")

        username_len = await reader.readexactly(1)
        self.log(f"Reading username of length {username_len[0]}")
        username = await reader.readexactly(username_len[0])
        self.log(f"Username: {username}")

        password_len = await reader.readexactly(1)
        self.log(f"Reading password of length {password_len[0]}")
        password = await reader.readexactly(password_len[0])
        self.log(f"Password: {password}")

        # Validate credentials
        if username.decode() != self.parent.username or password.decode() != self.parent.password:
            self.log("Authentication failed", MessageType.ERROR)
            writer.write(b"\x01\x01")  # Authentication failed
            await writer.drain()
            await self.close_writer(writer)
            return None

        # Send authentication success
        self.log("Authentication successful")
        writer.write(b"\x01\x00")  # Authentication successful
        await writer.drain()

        # Read SOCKS5 request
        self.log("Reading request")
        request = await reader.readexactly(4)
        if request[1] != 1:  # Only support CONNECT command
            self.log("Unsupported command", MessageType.ERROR)
            writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            await self.close_writer(writer)
            return None
        self.log("Received CONNECT command")

        return request


    def __del__(self):  # IMPORTANT: don't forget to decrement instances counter
        self.parent.instances -= 1
