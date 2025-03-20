from __future__ import annotations
import asyncio
import socket
from typing import Optional, Tuple, TYPE_CHECKING
from custom_types import HostData, UpstreamProxy, MessageType, global_log
from shared_storage import SharedStorage

if TYPE_CHECKING:
    from interlayer import ProxyInterlayer


class InterlayerInstance:
    """
    Handles a single client connection through the proxy interlayer.
    
    This class implements efficient bidirectional data transfer between
    client and upstream proxy using proper asyncio patterns.
    """
    def __init__(self, parent: ProxyInterlayer):
        self.parent = parent
        parent.instances += 1
        self.instance = parent.instances
        self.upstream: Optional[UpstreamProxy] = None
        self.closed_by_client = False
        self.host_data: Optional[HostData] = None
        self.request: Optional[bytes] = None

    def log(self, msg: str, msg_type=MessageType.DEBUG) -> None:
        """Log a message with the appropriate proxy instance identifier."""
        global_log(f"[PROXY:{self.parent.listen_port}:{self.instance}] {msg}", msg_type)

    async def pseudo_dns(self) -> None:
        """
        Converts domain name to IP address and back if needed.
        
        Instead of polling, this implementation efficiently uses async operations
        and properly handles exceptions.
        """
        ipv4_req = b"\x05\x01\x00\x01"
        host_req = b"\x05\x01\x00\x03"
        self.log(f"DEBUG: running pseudo DNS on {self.host_data.target_host}:{self.host_data.dst_port}")

        try:
            if self.host_data.target_host.startswith('223.'):
                real_host = await SharedStorage.ip_to_host(socket.inet_aton(self.host_data.target_host))
                self.host_data.target_host = real_host.decode()
                self.host_data.type = 3
        except Exception as e:
            self.log(f'Error in shared memory operations: {e}', MessageType.ERROR)

        # Build SOCKS request
        if self.host_data.type == 1:
            self.host_data.to_connect = ipv4_req + \
            socket.inet_aton(self.host_data.target_host) + self.host_data.dst_port.to_bytes(2, "big")
        elif self.host_data.type == 3:
            self.host_data.to_connect = host_req + bytes([len(self.host_data.target_host)]) +\
                self.host_data.target_host.encode() + self.host_data.dst_port.to_bytes(2, "big")

    async def proxy_data(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, 
                         label: str, buffer_size: int = 8192) -> None:
        """
        Efficiently proxy data from reader to writer without using timeouts or polling.
        
        Args:
            reader: Source stream reader
            writer: Destination stream writer
            label: Label for logging
            buffer_size: Size of the read buffer
        """
        try:
            while not reader.at_eof():
                data = await reader.read(buffer_size)
                if not data:
                    self.log(f'End of data stream {label}')
                    break

                writer.write(data)
                await writer.drain()
        except ConnectionResetError:
            if label == 'from_proxy':
                self.log(f'Connection reset by client', MessageType.INFO)
                self.closed_by_client = True
            else:
                self.log(f'Connection reset by proxy', MessageType.INFO)
        except Exception as e:
            self.log(f"Error proxying data {label}: {e}", MessageType.ERROR)

    async def handle_bidirectional_proxy(self, client_reader: asyncio.StreamReader, 
                                        client_writer: asyncio.StreamWriter) -> None:
        """
        Handle bidirectional data transfer between client and proxy.
        
        This implementation uses concurrent tasks for both directions 
        and properly handles cancellation.
        """
        if not self.upstream:
            return
            
        # Create tasks for both directions
        client_to_proxy = asyncio.create_task(
            self.proxy_data(client_reader, self.upstream.writer, 'to_proxy')
        )
        proxy_to_client = asyncio.create_task(
            self.proxy_data(self.upstream.reader, client_writer, 'from_proxy')
        )
        
        # Wait for either direction to complete or an error to occur
        try:
            done, pending = await asyncio.wait(
                [client_to_proxy, proxy_to_client],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Cancel the other task
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            
            # Check for any exceptions in completed tasks
            for task in done:
                exc = task.exception()
                if exc:
                    self.log(f"Task error: {exc}", MessageType.ERROR)
        except Exception as e:
            self.log(f"Error during bidirectional proxying: {e}", MessageType.ERROR)

    async def close_writer(self, writer: asyncio.StreamWriter) -> None:
        """Safely close a writer with proper error handling."""
        if writer and not writer.is_closing():
            try:
                writer.close()
                await writer.wait_closed()
            except Exception as e:
                self.log(f"Error closing connection: {e}", MessageType.ERROR)

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """
        Main handler for client connections.
        
        This optimized implementation uses proper async patterns and resource management.
        """
        client_addr = writer.get_extra_info('peername')
        self.log(f"New connection from {client_addr}")

        try:
            # Authenticate the client
            self.request = await self.client_auth(reader, writer)
            if not (self.request and isinstance(self.request, bytes) and len(self.request) == 4):
                return

            # Process the host connection details
            self.host_data = await self.handle_host_data(reader, writer)
            if not self.host_data:
                return

            # Process via pseudo DNS
            await self.pseudo_dns()

            # Authenticate with the upstream proxy
            self.upstream = await self.upstream_auth(writer)
            if not self.upstream:
                return

            # Start bidirectional proxying
            self.log(f"Proxying data for {self.host_data.target_host}:{self.host_data.dst_port}")
            await self.handle_bidirectional_proxy(reader, writer)

        except asyncio.CancelledError:
            self.log("Connection cancelled")
        except Exception as e:
            self.log(f"Error in client handler: {e}", MessageType.ERROR)
        finally:
            # Ensure all connections are properly closed
            await self.close_writer(writer)
            if self.upstream and self.upstream.writer:
                await self.close_writer(self.upstream.writer)
            self.log("Connection closed")

    async def handle_host_data(self, reader: asyncio.StreamReader, 
                              writer: asyncio.StreamWriter) -> Optional[HostData]:
        """Parse and extract host data from the client request."""
        addr_type = self.request[3]
        host_data = HostData(addr_type)

        try:
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
                return None

            self.log(f"Intercepted request: {host_data.target_host}:{host_data.dst_port}")
            return host_data
        except asyncio.IncompleteReadError:
            self.log("Connection closed during host data reading", MessageType.ERROR)
            return None
        except Exception as e:
            self.log(f"Error handling host data: {e}", MessageType.ERROR)
            return None

    async def upstream_auth(self, writer: asyncio.StreamWriter) -> Optional[UpstreamProxy]:
        """Authenticate with the upstream proxy with proper error handling."""
        self.log("Connecting to upstream proxy")
        
        try:
            # Connect to the upstream proxy
            upstream = UpstreamProxy()
            upstream.reader, upstream.writer = await asyncio.open_connection(
                self.parent.upstream_host, self.parent.upstream_port
            )

            # Forward authentication to upstream proxy
            self.log("Authenticating at upstream proxy")
            upstream.writer.write(b"\x05\x01\x02")
            await upstream.writer.drain()

            auth_response = await upstream.reader.readexactly(2)
            if auth_response[1] != 2:
                self.log("Upstream proxy does not support authentication", MessageType.ERROR)
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
                await self.close_writer(upstream.writer)
                return None
                
            self.log(f"Connected to {self.host_data.target_host}:{self.host_data.dst_port}")

            # Send successful response back to client
            writer.write(upstream_response)
            await writer.drain()
            return upstream
            
        except asyncio.IncompleteReadError:
            self.log("Connection closed during upstream authentication", MessageType.ERROR)
            if 'upstream' in locals() and upstream.writer:
                await self.close_writer(upstream.writer)
            return None
        except Exception as e:
            self.log(f"Error during upstream authentication: {e}", MessageType.ERROR)
            if 'upstream' in locals() and upstream.writer:
                await self.close_writer(upstream.writer)
            return None

    async def client_auth(self, reader: asyncio.StreamReader, 
                         writer: asyncio.StreamWriter) -> Optional[bytes]:
        """Authenticate the client with proper error handling."""
        try:
            # Read greeting (version + auth methods)
            self.log("Reading greeting")
            greeting = await reader.readexactly(2)
            if greeting[0] != 5:  # Ensure it's SOCKS5
                self.log("Invalid SOCKS version", MessageType.ERROR)
                await self.close_writer(writer)
                return None
                
            self.log("Valid SOCKS version 5")

            # Read authentication methods
            self.log("Reading auth methods")
            num_methods = greeting[1]
            methods = await reader.readexactly(num_methods)
            if 2 not in methods:  # Check if username/password auth is supported
                self.log(f"No acceptable auth methods: {methods}", MessageType.ERROR)
                writer.write(b"\x05\xFF")  # No acceptable methods
                await writer.drain()
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
                return None
                
            self.log("Valid authentication version 1")

            # Read username
            username_len = await reader.readexactly(1)
            self.log(f"Reading username of length {username_len[0]}")
            username = await reader.readexactly(username_len[0])
            
            # Read password
            password_len = await reader.readexactly(1)
            self.log(f"Reading password of length {password_len[0]}")
            password = await reader.readexactly(password_len[0])

            # Validate credentials
            if username.decode() != self.parent.username or password.decode() != self.parent.password:
                self.log("Authentication failed", MessageType.ERROR)
                writer.write(b"\x01\x01")  # Authentication failed
                await writer.drain()
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
                return None
                
            self.log("Received CONNECT command")
            return request
            
        except asyncio.IncompleteReadError:
            self.log("Connection closed during authentication", MessageType.ERROR)
            return None
        except Exception as e:
            self.log(f"Error during client authentication: {e}", MessageType.ERROR)
            return None

    def __del__(self):
        """Cleanup when the instance is garbage collected."""
        try:
            self.parent.instances -= 1
        except (AttributeError, TypeError):
            # Handle case where parent is already gone
            pass