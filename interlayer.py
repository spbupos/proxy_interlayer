import asyncio
import concurrent.futures
from typing import Optional, Dict, Any, Set
import time

from instance import InterlayerInstance
from shared_storage import SharedStorage
from custom_types import MessageType, global_log


class ProxyInterlayer:
    """
    Manages SOCKS5 proxy connections between clients and upstream proxies.
    
    This implementation uses efficient asyncio patterns and proper
    resource management.
    """
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
        
        # Track active tasks
        self.background_tasks: Set[asyncio.Task] = set()
        
        # Metrics
        self.start_time = time.time()
        self.connection_count = 0
        self.active_connections = 0
        self.connection_errors = 0
        
        self.instances = 0
        self.stopped = False
        self.server = None

        # Run server in background
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.server_future = self.executor.submit(self.start_server_sync)

    def log(self, message: str, msg_type=MessageType.DEBUG) -> None:
        """Log with proxy interlayer information."""
        global_log(f"[PROXY:{self.listen_port}:0] {message}", msg_type)

    async def handle_client_wrapper(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """
        Wrapper to handle a client connection.
        
        This implementation properly tracks tasks and handles cleanup.
        
        Args:
            reader: Client reader stream
            writer: Client writer stream
        """
        self.connection_count += 1
        self.active_connections += 1
        
        instance = InterlayerInstance(self)
        
        try:
            # Create and track the task
            task = asyncio.create_task(instance.handle_client(reader, writer))
            self.background_tasks.add(task)
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.connection_errors += 1
            self.log(f'Error in handle wrapper: {e}', MessageType.ERROR)
        finally:
            # Clean up
            if writer and not writer.is_closing():
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception as e:
                    self.log(f"Error closing writer: {e}", MessageType.ERROR)
                    
            # Remove task from tracking set
            for task in self.background_tasks.copy():
                if task.done():
                    self.background_tasks.remove(task)
                    
            self.active_connections -= 1
    
    async def shutdown_server(self):
        self.log("Shutting down proxy server...", MessageType.INFO)
            
        # Close server
        if self.server:
            self.server.close()
            await self.server.wait_closed()
                
        # Cancel any remaining tasks
        remaining_tasks = [t for t in self.background_tasks if not t.done()]
        if remaining_tasks:
            self.log(f"Cancelling {len(remaining_tasks)} remaining tasks", MessageType.INFO)
            for task in remaining_tasks:
                task.cancel()
                
            await asyncio.gather(*remaining_tasks, return_exceptions=True)
                
        self.log("Proxy server stopped", MessageType.INFO)

    async def start_server(self) -> None:
        """Start the proxy server."""
        SharedStorage.init()

        self.log(f'Starting proxy server on {self.listen_host}:{self.listen_port}', MessageType.INFO)
        self.server = await asyncio.start_server(
            self.handle_client_wrapper, 
            self.listen_host, 
            self.listen_port,
            backlog=100  # Allow up to 100 pending connections
        )
        
        server_task = asyncio.create_task(self.server.serve_forever())
        self.background_tasks.add(server_task)

        # Metrics reporting task
        metrics_task = asyncio.create_task(self._report_metrics())
        self.background_tasks.add(metrics_task)

        try:
            while not self.stopped:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def _report_metrics(self) -> None:
        """Periodically report metrics."""
        try:
            while not self.stopped:
                # Report every 5 minutes
                if int(time.time()) % 300 < 5:
                    metrics = self.get_metrics()
                    self.log(f"Metrics: {metrics}", MessageType.INFO)
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.log(f"Error in metrics reporting: {e}", MessageType.ERROR)

    def start_server_sync(self) -> None:
        """Start the server in a synchronous context."""
        retry_count = 0
        max_retries = 5
        retry_delay = 2
        
        while not self.stopped and retry_count < max_retries:
            try:
                asyncio.run(self.start_server())
                break
            except OSError:
                retry_count += 1
                self.log(f'Port {self.listen_port} is in use, retrying in {retry_delay}s (attempt {retry_count}/{max_retries})', 
                        MessageType.ERROR)
                import time
                time.sleep(retry_delay)
                retry_delay *= 1.5  # Exponential backoff
            except Exception as e:
                self.log(f"Error in main loop: {e}", MessageType.ERROR)
                if not self.stopped:
                    self.log("Restarting proxy server in 5 seconds...", MessageType.INFO)
                    import time
                    time.sleep(5)
                    
        if retry_count >= max_retries and not self.stopped:
            self.log(f"Failed to start proxy server after {max_retries} attempts", MessageType.ERROR)
            self.stopped = True

    def stop_server(self) -> None:
        """Stop the proxy server cleanly."""
        if self.stopped:
            return
            
        self.stopped = True
        asyncio.create_task(self.shutdown_server())
        
        # Wait for server to stop gracefully with timeout
        import time
        start_time = time.time()
        timeout = 10  # 10 seconds timeout
        
        while not self.server_future.done() and time.time() - start_time < timeout:
            time.sleep(0.1)
            
        if not self.server_future.done():
            self.log("Force stopping proxy server...", MessageType.INFO)
            
        self.executor.shutdown(wait=False)


    def get_metrics(self) -> Dict[str, Any]:
        """Get current proxy metrics."""
        uptime = time.time() - self.start_time
        
        metrics = {
            "uptime_seconds": int(uptime),
            "uptime_formatted": f"{int(uptime // 86400)}d {int((uptime % 86400) // 3600)}h {int((uptime % 3600) // 60)}m",
            "total_connections": self.connection_count,
            "active_connections": self.active_connections,
            "connection_errors": self.connection_errors,
            "active_tasks": len(self.background_tasks),
            "instances": self.instances
        }
        
        return metrics