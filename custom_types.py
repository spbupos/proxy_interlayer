from dataclasses import dataclass
import asyncio

# NOTICE: here:
# - target_host, dst_port - human-readable data
# - to_connect - binary data to send to upstream proxy
@dataclass
class HostData:
    type: int # (1 - IPv4, 3 - domain)
    dst_port: int = None
    target_host: str = None
    to_connect: bytes = None


@dataclass
class UpstreamProxy:
    reader: asyncio.StreamReader = None
    writer: asyncio.StreamWriter = None
