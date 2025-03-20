import asyncio
import concurrent.futures
import socket
from typing import Optional, Tuple, Dict, Any, List
from functools import lru_cache

from shared_storage import SharedStorage
from custom_types import MessageType, global_log


class PseudoDNSServer(asyncio.DatagramProtocol):
    """
    DNS server that responds to DNS requests with virtual IPs.
    
    This implementation includes proper error handling, caching,
    and efficient memory usage.
    """
    def __init__(self, port: int):
        super().__init__()
        self.port = port
        self.transport = None
        self.request_metrics = {
            "total_requests": 0,
            "successful_responses": 0,
            "failed_responses": 0,
            "invalid_requests": 0
        }
        # Cache for frequently requested domains (TTL-based)
        self._cache: Dict[bytes, Tuple[bytes, float]] = {}
        self._cache_ttl = 60  # 60 seconds TTL by default

    def log(self, message: str, msg_type=MessageType.DEBUG) -> None:
        """Log with DNS server information."""
        global_log(f"[DNS:{self.port}] {message}", msg_type)

    def connection_made(self, transport) -> None:
        """Called when connection is established."""
        self.transport = transport
        self.log(f"DNS server listening on port {self.port}", MessageType.INFO)

    def error_received(self, exception) -> None:
        """Handle datagram socket errors."""
        self.log(f'Datagram error: {exception}', MessageType.ERROR)
        self.request_metrics["failed_responses"] += 1

    def datagram_received(self, data: bytes, addr) -> None:
        """Process incoming DNS requests."""
        self.request_metrics["total_requests"] += 1
        asyncio.create_task(self.handle_request(data, addr))

    def connection_lost(self, exc) -> None:
        """Called when connection is lost."""
        if exc:
            self.log(f'Connection lost: {exc}', MessageType.ERROR)
        else:
            self.log('Connection closed', MessageType.INFO)

    @lru_cache(maxsize=128)
    def mark_unlinker(self, data: bytes, pos: int, mark_link: bool = False) -> Tuple[bytes, bool]:
        """
        Decode DNS domain name compression.
        
        This implementation uses LRU caching to improve performance for
        frequently requested domains.
        
        Args:
            data: The DNS message data
            pos: The position in the data
            mark_link: Whether the position is from a pointer
            
        Returns:
            Tuple of the domain name segment and whether it's a pointer
        """
        try:
            # If mark first 2 bits of byte is '00', mark is length, return next N bytes
            # If mark first 2 bits of byte is '11', mark is pointer, read next byte
            # and 14 bits after from '11' are position, recursively call this function
            mark_type = data[pos] >> 6
            
            if mark_type == 0:
                mark_length = data[pos]
                if mark_length == 0:  # finish
                    return b'', False
                return data[pos + 1: pos + 1 + mark_length], mark_link
                
            elif mark_type == 3:
                pointer = int.from_bytes(data[pos: pos + 2], 'big') & 0x3FFF
                if pointer >= len(data):
                    self.log(f"Invalid pointer: {pointer}", MessageType.ERROR)
                    return b'', False
                return self.mark_unlinker(data, pointer, True)
                
            else:
                self.log(f"Unsupported mark type: {mark_type}", MessageType.ERROR)
                return b'', False
        except IndexError:
            self.log(f"Index error in mark_unlinker at position {pos}", MessageType.ERROR)
            return b'', False
        except Exception as e:
            self.log(f"Error in mark_unlinker: {e}", MessageType.ERROR)
            return b'', False

    async def handle_request(self, data: bytes, addr) -> None:
        """
        Handle a DNS request.
        
        This method efficiently parses the DNS request, determines the hostname,
        and builds a response with a virtual IP.
        
        Args:
            data: The DNS request data
            addr: The address of the requester
        """
        try:
            # Parse the hostname from the request
            pos = 12  # Skip the header
            hostname = b''
            hostname_sub, mark_link = self.mark_unlinker(data, pos)
            hostname += hostname_sub

            while hostname_sub:
                if mark_link:
                    pos += 2
                else:
                    pos += len(hostname_sub) + 1
                
                hostname_sub, mark_link = self.mark_unlinker(data, pos)
                if hostname_sub:
                    hostname += b'.' + hostname_sub

            # Check cache first
            ip = await self._get_cached_ip(hostname)
            if not ip:
                # Resolve the hostname if not in cache
                ip = await SharedStorage.resolve_new_host(hostname)
                # Add to cache
                await self._add_to_cache(hostname, ip)

            # Build and send the response
            response = self.build_response(data, ip, pos+5, hostname)
            if response:
                self.transport.sendto(response, addr)
                self.request_metrics["successful_responses"] += 1
            else:
                self.request_metrics["failed_responses"] += 1
                
        except Exception as e:
            self.log(f"Error handling DNS request: {e}", MessageType.ERROR)
            self.request_metrics["failed_responses"] += 1

    async def _get_cached_ip(self, hostname: bytes) -> Optional[bytes]:
        """
        Get an IP from cache if available and not expired.
        
        Args:
            hostname: The hostname to look up
            
        Returns:
            The cached IP or None if not in cache or expired
        """
        import time
        
        cache_entry = self._cache.get(hostname)
        if cache_entry:
            ip, expiry = cache_entry
            if time.time() < expiry:
                return ip
            # Remove expired entry
            del self._cache[hostname]
        return None

    async def _add_to_cache(self, hostname: bytes, ip: bytes) -> None:
        """
        Add a hostname->IP mapping to the cache.
        
        Args:
            hostname: The hostname
            ip: The IP address
        """
        import time
        
        # Simple LRU mechanism - if cache gets too big, remove oldest entries
        if len(self._cache) > 1000:
            # Remove 20% of oldest entries
            items_to_remove = sorted(
                self._cache.items(), 
                key=lambda x: x[1][1]
            )[:200]
            for key, _ in items_to_remove:
                del self._cache[key]
                
        # Add to cache with expiry time
        self._cache[hostname] = (ip, time.time() + self._cache_ttl)

    def build_response(self, request: bytes, ip: bytes, garbage_start: int, hostname: bytes) -> Optional[bytes]:
        """
        Build a DNS response packet.
        
        This implementation validates inputs and handles edge cases properly.
        
        Args:
            request: The original DNS request
            ip: The IP address to return
            garbage_start: Position where the question section ends
            hostname: The hostname (for logging)
            
        Returns:
            The DNS response packet
        """
        try:
            # Validate inputs
            #if garbage_start <= 0 or garbage_start >= len(request):
            #    self.log(f"Invalid garbage_start: {garbage_start}", MessageType.ERROR)
            #    return None
                
            if not ip or len(ip) != 4:
                self.log(f"Invalid IP: {ip}", MessageType.ERROR)
                return None

            # Construct the DNS response header
            # \x81\x80: Standard DNS response flags (QR=1, Opcode=0, AA=1, TC=0, RD=1, RA=1, Z=0, RCODE=0)
            header = request[:2] + b'\x81\x80'  # Response flags
            question = request[12:garbage_start]

            if len(question) < 4:
                self.log(f"Question section too short: {len(question)}", MessageType.ERROR)
                return None

            type_int = int.from_bytes(question[-4:-2], 'big')
            
            # HOOK: we're supporting only IPv4 (1), so AAAA (28) and other
            # are dropped, but if we don't return anything or return response for A
            # client programs dropping it, so we should return empty answer on non-A
            if type_int != 1:
                header += request[4:6] + b'\x00\x00' + request[8:12]  # count of sections
                answer = b''
            else:
                header += request[4:6] + request[4:6] + request[8:12]
                # Construct the DNS answer section
                # \xc0\x0c: Pointer to the domain name in the question (mark '11', instead of copying)
                # \x00\x01\x00\x01: Type A (host address) and Class IN (Internet) - last four bytes in the question
                # \x00\x00\x00\x3c: TTL (60 seconds)
                # \x00\x04: Length of the IP address (4 bytes)
                type_class = question[-4:]
                answer = b'\xc0\x0c' + type_class + b'\x00\x00\x00\x0f\x00\x04' + ip

            result = header + question + answer
            
            # Only add garbage if it exists and is valid
            if garbage_start < len(request):
                result += request[garbage_start:]

            self.log(f'Resolving {hostname.decode()} to {socket.inet_ntoa(ip)}')
            return result
            
        except Exception as e:
            self.log(f"Error building DNS response: {e}", MessageType.ERROR)
            return None

    def get_metrics(self) -> Dict[str, Any]:
        """Get current metrics."""
        metrics = self.request_metrics.copy()
        metrics["cache_size"] = len(self._cache)
        return metrics


class DNSWrapper:
    """
    Wrapper to manage the DNS server lifecycle.
    
    This implementation includes proper shutdown handling and
    metrics collection.
    """
    def __init__(self, endpoint: str = '127.0.0.1', port: int = 1053):
        self.endpoint = endpoint
        self.port = port
        self.stopped = False
        self.dns_server = None
        self.transport = None
        
        # Run server in background
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.server_future = self.executor.submit(self.start_server_sync)

    def log(self, message: str, msg_type=MessageType.DEBUG) -> None:
        """Log with DNS wrapper information."""
        global_log(f"[DNS:{self.port}] {message}", msg_type)

    async def start_server(self) -> None:
        """Start the DNS server."""
        try:
            SharedStorage.init()

            loop = asyncio.get_running_loop()
            self.log(f"Starting DNS server on {self.endpoint}:{self.port}", MessageType.INFO)
            
            # Create the DNS server protocol
            self.dns_server = PseudoDNSServer(self.port)
            self.transport, _ = await loop.create_datagram_endpoint(
                lambda: self.dns_server, 
                local_addr=(self.endpoint, self.port)
            )

            # Keep the server running until stopped
            while not self.stopped:
                await asyncio.sleep(1)
                
                # Periodically log metrics (every 5 minutes)
                if hasattr(self.dns_server, 'request_metrics') and int(asyncio.get_event_loop().time()) % 300 == 0:
                    metrics = self.dns_server.get_metrics()
                    self.log(f"Metrics: {metrics}", MessageType.INFO)

        except asyncio.CancelledError:
            self.log("Server task cancelled", MessageType.INFO)
        except Exception as e:
            self.log(f"Error in DNS server: {e}", MessageType.ERROR)
        finally:
            # Clean up resources
            if self.transport:
                self.transport.close()
            self.log("DNS server stopped", MessageType.INFO)

    def start_server_sync(self) -> None:
        """Start the server in a synchronous context."""
        retry_count = 0
        max_retries = 5
        retry_delay = 2
        
        while not self.stopped and retry_count < max_retries:
            try:
                asyncio.run(self.start_server())
                break
            except OSError as e:
                retry_count += 1
                self.log(f'Port {self.port} is in use, retrying in {retry_delay}s (attempt {retry_count}/{max_retries})', 
                        MessageType.ERROR)
                import time
                time.sleep(retry_delay)
                retry_delay *= 1.5  # Exponential backoff
            except Exception as e:
                self.log(f"Error in main DNS loop: {e}", MessageType.ERROR)
                if not self.stopped:
                    self.log("Restarting DNS server in 5 seconds...", MessageType.INFO)
                    import time
                    time.sleep(5)
                    
        if retry_count >= max_retries and not self.stopped:
            self.log(f"Failed to start DNS server after {max_retries} attempts", MessageType.ERROR)
            self.stopped = True

    def stop_server(self) -> None:
        """Stop the DNS server cleanly."""
        self.log("Stopping DNS server...", MessageType.INFO)
        self.stopped = True
        
        # Wait for server to stop gracefully with timeout
        import time
        start_time = time.time()
        timeout = 5  # 5 seconds timeout
        
        while not self.server_future.done() and time.time() - start_time < timeout:
            time.sleep(0.1)
            
        if not self.server_future.done():
            self.log("Force stopping DNS server...", MessageType.WARNING)
            
        self.executor.shutdown(wait=False)
        self.log("DNS server shutdown complete", MessageType.INFO)

    def get_metrics(self) -> Dict[str, Any]:
        """Get DNS server metrics."""
        if self.dns_server and hasattr(self.dns_server, 'get_metrics'):
            return self.dns_server.get_metrics()
        return {"status": "unknown"}