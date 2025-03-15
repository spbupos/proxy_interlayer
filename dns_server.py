import asyncio
import concurrent.futures
from shared_storage import SharedStorage

class PseudoDNSServer(asyncio.DatagramProtocol):
    def __init__(self):
        super().__init__()

    def connection_made(self, transport):
        self.transport = transport

    def error_received(self, exception):
        print(f'Datagram error: {exception}')

    def datagram_received(self, data, addr):
        asyncio.create_task(self.handle_request(data, addr))

    def mark_unlinker(self, data: bytes, pos, mark_link=False) -> tuple[bytes, bool]:
        # if mark first 2 bits of byte is '00', mark is length, return next N bytes
        # if mark first 2 bits of byte is '11', mark is pointer, read next byte
        # and 14 bits after from '11' are position, recursively call this function
        mark_type = data[pos] >> 6
        if mark_type == 0:
            mark_length = data[pos]
            if mark_length == 0: # finish
                return b'', False
            return data[pos + 1: pos + 1 + mark_length], mark_link
        elif mark_type == 3:
            pointer = int.from_bytes(data[pos: pos + 2], 'big') & 0x3FFF
            return self.mark_unlinker(data, pointer, True)

    async def handle_request(self, data: bytes, addr):
        pos = 12
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

        ip = await SharedStorage.resolve_new_host(hostname)
        #await SharedStorage.print_shm()

        # HOOK: after reading zone (ru/ua/com) we are at position of 00 after
        # domain name, after we have QTYPE and QCLASS (4 bytes). By standard,
        # this is *ALWAYS* end of request, but actually we sometimes have
        # garbage (additional section) after this. We should pass to response
        # them splitted - last byte of response is pos+4, first byte of
        # additional sections (garbage) is pos+5, due to slices in python
        # both times we should pass pos+5
        response = self.build_response(data, ip, pos+5)
        self.transport.sendto(response, addr)

    def build_response(self, request: bytes, ip: bytes, garbage_start: int) -> bytes:
        # Construct the DNS response header
        # \x81\x80: Standard DNS response flags (QR=1, Opcode=0, AA=1, TC=0, RD=1, RA=1, Z=0, RCODE=0)
        header = request[:2] + b'\x81\x80'  # Response flags
        header += request[4:6] + request[4:6] + request[8:12] # count of sections (same as in the request)
        question = request[12:garbage_start]

        # Construct the DNS answer section
        # \xc0\x0c: Pointer to the domain name in the question (mark '11', instead of copying)
        # \x00\x01\x00\x01: Type A (host address) and Class IN (Internet) - last four bytes in the question
        # \x00\x00\x00\x3c: TTL (60 seconds)
        # \x00\x04: Length of the IP address (4 bytes)
        type_class = question[-4:]
        answer = b'\xc0\x0c' + type_class + b'\x00\x00\x00\x0f\x00\x04' + ip

        result = header + question + answer + request[garbage_start:]
        print(f'DEBUG: request {request.hex()}, response {result.hex()}')
        return result


class DNSWrapper:
    def __init__(self, endpoint='127.0.0.1', port=1053):
        self.endpoint = endpoint
        self.port = port
        self.stop_event = None

        # run server in background
        executor = concurrent.futures.ThreadPoolExecutor()
        executor.submit(self.start_server_sync)

    def log(self, message):
        print(f"[DNS:{self.port}] {message}")

    async def start_server(self):
        self.stop_event = asyncio.Event()
        SharedStorage.init()

        loop = asyncio.get_running_loop()
        self.log(f"Starting DNS server on {self.endpoint}:{self.port}")
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: PseudoDNSServer(), local_addr=(self.endpoint, self.port)
        )

        try:
            await self.stop_event.wait()
        except Exception as e:
            self.log(f"Error on waiting event: {e}")

        self.log("Shutting down DNS server...")
        transport.close()
        self.log("DNS server stopped")

    def start_server_sync(self):
        while True:
            try:
                asyncio.run(self.start_server())
            except Exception as e:
                self.log(f"Error: {e}")

    def stop_server(self):
        self.stop_event.set()


def main():
    DNSWrapper("0.0.0.0", 1053)
    print('Hello, world!')


if __name__ == "__main__":
    main()
