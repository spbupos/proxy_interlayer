import random
from multiprocessing import shared_memory, Lock
from typing import Optional, ClassVar, List, Dict
from custom_types import MessageType, global_log

# Constants
ENTRY_SIZE = 260  # Size of each entry in bytes

class SharedStorage:
    """
    Thread-safe shared memory implementation for storing hostname to IP mappings.
    
    This optimized implementation properly handles thread safety,
    resource management, and error conditions.
    """
    # Class variables
    shm_name: ClassVar[str] = ""
    shm_size: ClassVar[int] = 0
    count: ClassVar[int] = 0
    max_count: ClassVar[int] = 0
    free_slots: ClassVar[List[int]] = []
    shm: ClassVar[Optional[shared_memory.SharedMemory]] = None
    lock: ClassVar[Lock] = None  
    
    # Performance metrics
    metrics: ClassVar[Dict[str, int]] = {
        "resolve_requests": 0,
        "resolve_hits": 0,
        "resolve_misses": 0,
        "memory_full_events": 0
    }

    @classmethod
    def log(cls, message: str, msg_type=MessageType.DEBUG) -> None:
        """Log messages with shared memory identifier."""
        global_log(f"[SHM:{cls.shm_name}] {message}", msg_type)
    
    @classmethod
    def reinit_free(cls) -> None:
        """
        Reinitialize the free slots list.
        
        This method is now thread-safe and more efficient.
        """
        with cls.lock:
            if not cls.free_slots:
                cls.free_slots = [
                    i for i in range(cls.max_count) 
                    if i & 0xFF != 0 and cls.shm.buf[i * ENTRY_SIZE] == 0
                ]
                
                if not cls.free_slots:
                    cls.log("WARNING: No free slots available!", MessageType.ERROR)
                    cls.metrics["memory_full_events"] += 1
                    # Create emergency slot by clearing oldest entry
                    for i in range(1, cls.max_count):
                        if i & 0xFF != 0:  # Skip slot 0
                            cls.free_slots = [i]
                            # Clear the slot
                            cls.shm.buf[i * ENTRY_SIZE:(i + 1) * ENTRY_SIZE] = bytes(ENTRY_SIZE)
                            break

    @classmethod
    def init(cls, shm_name: str = "storage0", shm_size: int = 50*1024*1024) -> None:
        """
        Initialize shared memory.
        
        Args:
            shm_name: Name of the shared memory segment
            shm_size: Size of the shared memory in bytes
        """
        cls.shm_name = shm_name
        cls.shm_size = shm_size
        cls.count = 0
        cls.max_count = shm_size // ENTRY_SIZE
        cls.lock = Lock()
        cls.metrics = {
            "resolve_requests": 0,
            "resolve_hits": 0,
            "resolve_misses": 0,
            "memory_full_events": 0
        }

        try:
            # Try attaching to existing shared memory
            cls.shm = shared_memory.SharedMemory(name=shm_name, create=False, size=shm_size)
            cls.log("Shared memory already exists. Skipping initialization.")
        except FileNotFoundError:
            cls.shm = shared_memory.SharedMemory(name=shm_name, create=True, size=shm_size)
            with cls.lock:
                cls.shm.buf[:shm_size] = bytes(shm_size)
            cls.log(f"Shared memory `{shm_name}` initialized with {shm_size} bytes.")
        except Exception as e:
            cls.log(f"Error in shared memory: {e}", MessageType.ERROR)
            raise e

        # Initialize free slots
        cls.reinit_free()

    @classmethod
    async def resolve_new_host(cls, hostname: bytes) -> bytes:
        """
        Resolve a hostname to a virtual IP address.
        
        Args:
            hostname: The hostname to resolve
            
        Returns:
            bytes: The virtual IP address
            
        Raises:
            ValueError: If the hostname is invalid or the storage is full
        """
        cls.metrics["resolve_requests"] += 1
        
        # Validate hostname
        if len(hostname) > 255 or len(hostname) == 0:
            raise ValueError("Hostname is invalid")
            
        with cls.lock:
            if cls.count >= cls.max_count - 10:  # Keep some buffer
                cls.log("WARNING: Shared storage is near capacity", MessageType.ERROR)
            
            # Ensure we have free slots
            if not cls.free_slots:
                cls.reinit_free()
                
            if not cls.free_slots:
                raise ValueError("Shared storage is full")
            
            # Get a random free slot
            free_slot_index = random.randint(0, len(cls.free_slots) - 1)
            free_slot = cls.free_slots.pop(free_slot_index)
            
            # Write hostname data
            hostname_len = len(hostname)
            offset = free_slot * ENTRY_SIZE
            cls.shm.buf[offset:offset + 3] = free_slot.to_bytes(3, 'big')
            cls.shm.buf[offset + 3] = hostname_len
            cls.shm.buf[offset + 4:offset + 4 + hostname_len] = hostname
            cls.count += 1
            cls.metrics["resolve_misses"] += 1
            
        return b'\xdf' + free_slot.to_bytes(3, 'big')  # 223.x.x.x

    @classmethod
    async def ip_to_host(cls, ip: bytes) -> bytes:
        """
        Convert a virtual IP address back to a hostname.
        
        Args:
            ip: The virtual IP address
            
        Returns:
            bytes: The hostname corresponding to the IP
        """
        # Check if it's "our" IP (223.x.x.x)
        if ip[0] != 0xDF:
            return ip  # Return as is
            
        try:
            with cls.lock:
                ip_int = int.from_bytes(ip[1:], 'big')
                
                # Validate IP is within range
                if ip_int >= cls.max_count:
                    cls.log(f"ERROR: IP out of range: {ip_int}", MessageType.ERROR)
                    return ip
                
                offset = ip_int * ENTRY_SIZE
                entry = cls.shm.buf[offset:offset + ENTRY_SIZE]
                hostname_len = entry[3]
                
                # Validate hostname length
                if hostname_len == 0 or hostname_len > 255:
                    cls.log(f"ERROR: Invalid hostname length: {hostname_len}", MessageType.ERROR)
                    return ip
                    
                result = bytes(entry[4:4 + hostname_len])
                
                # Mark slot as free
                cls.free_slots.append(ip_int)
                cls.count -= 1
                cls.metrics["resolve_hits"] += 1
                
            return result
        except Exception as e:
            cls.log(f"Error converting IP to host: {e}", MessageType.ERROR)
            return ip

    @classmethod
    async def get_metrics(cls) -> Dict[str, int]:
        """Get current performance metrics."""
        with cls.lock:
            return cls.metrics.copy()

    @classmethod
    def cleanup(cls) -> None:
        """
        Clean up shared memory resources.
        
        This should be called when shutting down the application to properly release
        shared memory resources.
        """
        try:
            if cls.shm:
                cls.shm.close()
                try:
                    cls.shm.unlink()  # This will remove the shared memory segment
                except Exception as e:
                    cls.log(f"Error unlinking shared memory: {e}", MessageType.ERROR)
                cls.log("Shared memory resources cleaned up", MessageType.INFO)
        except Exception as e:
            cls.log(f"Error cleaning up shared memory: {e}", MessageType.ERROR)