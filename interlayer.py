import asyncio
import concurrent.futures
from instance import InterlayerInstance
from shared_storage import SharedStorage
from custom_types import MessageType, global_log

class ProxyInterlayer:
    def __init__(self,
                upstream_host: str,
                upstream_port: int,
                username: str,
                password: str,
                listen_host: str,
                listen_port: int):

        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.username = username
        self.password = password
        self.listen_host = listen_host
        self.listen_port = listen_port

        self.instances = 0
        self.stopped = False

        # run server in background
        executor = concurrent.futures.ThreadPoolExecutor()
        executor.submit(self.start_server_sync)

    def log(self, message, msg_type=MessageType.DEBUG):
        # interlayer instance log contains number of instance
        # from 1 to infinity, so main controller is number 0
        global_log(f"[PROXY:{self.listen_port}:0] {message}", msg_type)

    async def handle_client_wrapper(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        instance = InterlayerInstance(self)
        try:
            return await instance.handle_client(reader, writer)
        except (asyncio.CancelledError, GeneratorExit):
            pass
        except Exception as e:
            self.log(f'Error in handle wrapper: {e}', MessageType.ERROR)
        finally:
            if writer and not writer.is_closing():
                writer.close()
                await writer.wait_closed()

    async def start_server(self):
        SharedStorage.init()

        self.log(f'Starting proxy server on {self.listen_host}:{self.listen_port}', MessageType.INFO)
        server = await asyncio.start_server(self.handle_client_wrapper, self.listen_host, self.listen_port)
        server_task = asyncio.create_task(server.serve_forever())

        while not self.stopped:
            await asyncio.sleep(1)

        self.log("Shutting down proxy server...", MessageType.INFO)
        server.close()
        await server.wait_closed()
        server_task.cancel()
        try:
            await server_task  # Ensure it's properly cancelled
        except asyncio.CancelledError:
            pass

        self.log("Proxy server stopped", MessageType.INFO)

    def start_server_sync(self):
        while not self.stopped:
            try:
                asyncio.run(self.start_server())
            except OSError:  # port already in use
                self.log('Port is in use, exiting...', MessageType.ERROR)
                self.stopped = True
            except Exception as e:
                self.log(f"Error in main loop: {e}", MessageType.ERROR)

    def stop_server(self):
        self.stopped = True
