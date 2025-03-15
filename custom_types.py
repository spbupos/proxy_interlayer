from dataclasses import dataclass
from enum import Enum
import asyncio
from datetime import datetime

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

# create types of messages
class MessageType(Enum):
    ERROR = 1
    INFO = 2
    DEBUG = 3

LOGLEVEL = MessageType.DEBUG

def global_log(msg: str, msg_type=MessageType.DEBUG):
    if msg_type.value <= LOGLEVEL.value:
        # add current time in YYYY-MM-DD HH:MM:SS.%f format
        print(f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")}] {msg}')
