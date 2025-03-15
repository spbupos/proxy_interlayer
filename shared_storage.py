import asyncio
import heapq
from multiprocessing import shared_memory, Lock

ENTRY_SIZE = 260

class SharedStorage:
    shm_name: str
    shm_size: int
    count: int
    max_count: int
    free_slots: list = []
    shm: shared_memory.SharedMemory
    lock: Lock

    @classmethod
    def init(cls, shm_name="storage0", shm_size=1048576):
        cls.shm_name = shm_name
        cls.shm_size = shm_size
        cls.count = 0
        cls.max_count = shm_size // ENTRY_SIZE
        cls.lock = Lock()

        try:
            # Try attaching to existing shared memory
            cls.shm = shared_memory.SharedMemory(name=shm_name, create=False, size=shm_size)
            print("Shared memory already exists. Skipping initialization.")
        except FileNotFoundError:
            cls.shm = shared_memory.SharedMemory(name=shm_name, create=True, size=shm_size)
            with cls.lock:
                cls.shm.buf[:shm_size] = bytes(shm_size)
            print(f"Shared memory `{shm_name}` initialized with {shm_size} bytes.")

        # HOOK: free slots only where last byte != 0 and first byte at position == 0 (empty)
        if not cls.free_slots:
            cls.free_slots = [i for i in range(cls.max_count) if i & 0xFF != 0 and cls.shm.buf[i * ENTRY_SIZE] == 0]

    # entry - 260 bytes (ENTRY_SIZE)
    # IP address 3 bytes (X.X.X)
    # hostname length - 1 byte (0-255)
    # hostname (string) - 256 bytes (RFC 3986)
    # NOTICE: functions works only with bytes representations
    @classmethod
    async def resolve_new_host(cls, hostname: bytes) -> bytes:
        # check if hostname length is capable with RFC 3986
        if len(hostname) > 255 or len(hostname) == 0:
            raise ValueError("Hostname is invalid")
        if cls.count >= cls.max_count:
            raise ValueError("Shared storage is full")

        # write new entry
        #with cls.lock:
        free_slot = heapq.heappop(cls.free_slots)
        hostname_len = len(hostname)
        cls.shm.buf[free_slot * ENTRY_SIZE: free_slot * ENTRY_SIZE + 3] = free_slot.to_bytes(3, 'big')
        cls.shm.buf[free_slot * ENTRY_SIZE + 3] = hostname_len
        cls.shm.buf[free_slot * ENTRY_SIZE + 4: free_slot * ENTRY_SIZE + 4 + hostname_len] = hostname
        cls.count += 1

        return b'\xdf' + free_slot.to_bytes(3, 'big') # 223 -> 0xE1

    @classmethod
    async def ip_to_host(cls, ip: bytes) -> bytes:
        # check it's "our" (223.X.X.X) IP
        if not ip[0] != b'\xdf':
            return ip # return as is

        ip_int = int.from_bytes(ip[1:], 'big')
        print(f'DEBUG: ip_int={ip_int}')
        entry = cls.shm.buf[ip_int * ENTRY_SIZE: (ip_int + 1) * ENTRY_SIZE]
        hostname_len = entry[3]

        #with cls.lock:
        heapq.heappush(cls.free_slots, ip_int)
        cls.count -= 1
        # NOTICE: we don't actually overwrite memory after deletion, it's just marked as free

        return bytes(entry[4:4 + hostname_len])

    @classmethod
    async def print_shm(cls):
        print(f"Shared memory `{cls.shm_name}`:")
        for i in range(cls.max_count):
            entry = cls.shm.buf[i * ENTRY_SIZE: (i + 1) * ENTRY_SIZE]
            ip_bytes = b'\xdf' + entry[:3]
            if ip_bytes[3] == 0:
                continue
            hostname_len = entry[3]
            hostname = bytes(entry[4:4 + hostname_len])
            print(f"{ip_bytes} -> {hostname}")

async def main():
    # testing
    SharedStorage.init()
    ip1 = await SharedStorage.resolve_new_host(b"google.com")
    ip2 = await SharedStorage.resolve_new_host(b"yandex.ru")
    ip3 = await SharedStorage.resolve_new_host(b"vk.com")
    await SharedStorage.print_shm()
    host1 = await SharedStorage.ip_to_host(ip1)
    print(f'225.0.0.1: {host1}')
    ip4 = await SharedStorage.resolve_new_host(b"mail.ru")
    await SharedStorage.print_shm()

if __name__ == "__main__":
    asyncio.run(main())
