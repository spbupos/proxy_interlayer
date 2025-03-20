import time
import json
import socket
import asyncio
from typing import Dict, Any, Optional, List
import fastapi
import uvicorn
from pydantic import BaseModel, Field

from interlayer import ProxyInterlayer
from dns_server import DNSWrapper
from shared_storage import SharedStorage
import custom_types


# Define API request models
class ProxyInstanceRequest(BaseModel):
    """Model for proxy instance creation requests."""
    upstream_host: str
    upstream_port: int
    username: str
    password: str
    listen_port: int
    listen_host: str = "0.0.0.0"


class StopInstanceRequest(BaseModel):
    """Model for stopping proxy instances."""
    listen_port: int
    listen_host: str = "0.0.0.0"


class APIController:
    """
    Control API for managing proxy instances.
    
    This implementation includes proper request validation, resource management,
    and metrics reporting.
    """
    def __init__(self, config_file: str = "proxy.json", api_host: str = "0.0.0.0", api_port: int = 1984):
        self.fastapi_app = fastapi.FastAPI(
            title="Proxy Control API",
            description="API for managing proxy instances",
            version="1.0.0"
        )
        self.config_file = config_file

        self.config_dict: Dict[str, Any] = {}
        self.port_instances: Dict[int, ProxyInterlayer] = {}
        self.dns_instance: Optional[DNSWrapper] = None

        self.dns_default_host = "0.0.0.0"
        self.dns_default_port = 1053

        self.api_host = api_host
        self.api_port = api_port
        
        # Metrics
        self.start_time = time.time()

        # Initialize components
        self.init_api()
        self.init_methods()

    @staticmethod
    def is_udp_free(host: str, port: int) -> bool:
        """
        Check if a UDP port is available on the given host.
        
        Args:
            host: The host to check
            port: The port to check
            
        Returns:
            bool: True if the port is free, False otherwise
        """
        try:
            udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_socket.bind((host, port))
            udp_socket.close()
            return True  # Port is free
        except OSError:
            return False  # Port is in use

    def add_instance_to_dict(self, upstream_host: str, upstream_port: int, 
                            username: str, password: str, 
                            listen_host: str, listen_port: int) -> None:
        """
        Add a proxy instance configuration to the config dictionary.
        
        Args:
            upstream_host: Upstream proxy host
            upstream_port: Upstream proxy port
            username: Authentication username
            password: Authentication password
            listen_host: Local host to listen on
            listen_port: Local port to listen on
        """
        self.config_dict['proxy_instances'][str(listen_port)] = {
            "upstream_host": upstream_host,
            "upstream_port": upstream_port,
            "username": username,
            "password": password,
            "listen_host": listen_host,
            "listen_port": listen_port,
            "created_at": time.time()
        }
        try:
            with open(self.config_file, "w") as f:
                json.dump(self.config_dict, f, indent=2)
        except Exception as e:
            print(f"Error writing config: {e}")

    def remove_instance_from_dict(self, listen_port: int) -> None:
        """
        Remove a proxy instance configuration from the config dictionary.
        
        Args:
            listen_port: The port of the instance to remove
        """
        if str(listen_port) in self.config_dict['proxy_instances']:
            self.config_dict['proxy_instances'].pop(str(listen_port))
            try:
                with open(self.config_file, "w") as f:
                    json.dump(self.config_dict, f, indent=2)
            except Exception as e:
                print(f"Error writing config: {e}")

    def init_methods(self) -> None:
        """Initialize the FastAPI endpoints."""
        
        # Health check endpoint
        @self.fastapi_app.get("/health")
        async def health_check():
            """Check if the API is running."""
            return {"status": "ok", "uptime": int(time.time() - self.start_time)}

        # Get all instances
        @self.fastapi_app.get("/instances")
        async def get_instances():
            """Get all active proxy instances."""
            instances = {}
            for port, instance in self.port_instances.items():
                instances[str(port)] = {
                    "upstream_host": instance.upstream_host,
                    "upstream_port": instance.upstream_port,
                    "listen_host": instance.listen_host,
                    "listen_port": instance.listen_port,
                    "active_connections": instance.active_connections,
                    "total_connections": instance.connection_count
                }
            return {"instances": instances}

        # Get metrics
        @self.fastapi_app.get("/metrics")
        async def get_metrics():
            """Get system metrics."""
            metrics = {
                "uptime": int(time.time() - self.start_time),
                "proxy_instances": len(self.port_instances),
                "dns_status": "active" if self.dns_instance and not self.dns_instance.stopped else "inactive"
            }
            
            # Add DNS metrics if available
            if self.dns_instance:
                dns_metrics = self.dns_instance.get_metrics()
                metrics["dns"] = dns_metrics
                
            # Add SharedStorage metrics
            storage_metrics = asyncio.run(SharedStorage.get_metrics())
            metrics["storage"] = storage_metrics
            
            return metrics

        # Start a new proxy instance
        @self.fastapi_app.post("/start_instance")
        async def start_instance(request: ProxyInstanceRequest):
            """
            Start a new proxy instance.
            
            Args:
                request: The instance configuration
                
            Returns:
                Information about the started instance
            """
            upstream_host = request.upstream_host
            upstream_port = request.upstream_port
            username = request.username
            password = request.password
            listen_host = request.listen_host
            listen_port = request.listen_port

            # Check if port is already in use
            if listen_port in self.port_instances.keys():
                return fastapi.Response(
                    content=json.dumps({"error": "Instance already active on this port"}), 
                    status_code=400
                )

            # Check if port is available
            if not self.is_udp_free(listen_host, listen_port):
                return fastapi.Response(
                    content=json.dumps({"error": f"Port {listen_port} is in use by another process"}), 
                    status_code=400
                )

            # Create new proxy instance
            try:
                proxy = ProxyInterlayer(upstream_host, upstream_port, username, password, listen_host, listen_port)
                self.port_instances[listen_port] = proxy
                self.add_instance_to_dict(upstream_host, upstream_port, username, password, listen_host, listen_port)
                return {
                    "status": "success", 
                    "message": f"Instance started on {listen_host}:{listen_port}",
                    "instance": {
                        "upstream_host": upstream_host,
                        "upstream_port": upstream_port,
                        "listen_host": listen_host,
                        "listen_port": listen_port
                    }
                }
            except Exception as e:
                return fastapi.Response(
                    content=json.dumps({"error": f"Error occurred: {str(e)}"}), 
                    status_code=500
                )

        # Stop a proxy instance
        @self.fastapi_app.post("/stop_instance")
        async def stop_instance(request: StopInstanceRequest):
            """
            Stop a running proxy instance.
            
            Args:
                request: The instance to stop
                
            Returns:
                Status of the operation
            """
            listen_host = request.listen_host
            listen_port = request.listen_port

            if listen_port not in self.port_instances:
                return fastapi.Response(
                    content=json.dumps({"error": "Instance not found"}), 
                    status_code=404
                )

            if self.port_instances[listen_port].stopped:
                return fastapi.Response(
                    content=json.dumps({"error": "Instance already stopped"}), 
                    status_code=400
                )

            # Stop the instance
            self.port_instances[listen_port].stop_server()
            self.port_instances.pop(listen_port)
            self.remove_instance_from_dict(listen_port)
            
            return {
                "status": "success", 
                "message": f"Instance stopped on {listen_host}:{listen_port}"
            }

        # Set log level
        @self.fastapi_app.post("/set_loglevel")
        async def set_loglevel(level: int = fastapi.Query(..., ge=0, le=3)):
            """
            Set the global log level.
            
            Args:
                level: Log level (0-3)
                
            Returns:
                New log level
            """
            old_level = custom_types.LOGLEVEL
            custom_types.LOGLEVEL = level
            
            # Update config
            self.config_dict["loglevel"] = level
            try:
                with open(self.config_file, "w") as f:
                    json.dump(self.config_dict, f, indent=2)
            except Exception as e:
                return fastapi.Response(
                    content=json.dumps({"error": f"Error writing config: {str(e)}"}), 
                    status_code=500
                )
            
            return {
                "status": "success", 
                "old_level": old_level, 
                "new_level": level
            }

    def init_api(self) -> None:
        """Initialize the API and load configuration."""
        # Read config file
        try:
            with open(self.config_file, "r") as f:
                self.config_dict = json.load(f)
        except FileNotFoundError:
            print("Config file not found. Creating new...")
            self.config_dict = {"loglevel": 2, "proxy_instances": {}, "dns_port": 1053}
            try:
                with open(self.config_file, "w") as f:
                    json.dump(self.config_dict, f, indent=2)
            except Exception as e:
                print(f"Can't init config file: {e}")
        except Exception as e:
            print(f"Error reading config: {e}")
            exit(1)

        # Initialize shared storage
        SharedStorage.init()

        # Start DNS server
        dns_port = self.config_dict.get("dns_port", self.dns_default_port)

        if not self.is_udp_free(self.dns_default_host, dns_port):
            print(f"Warning: DNS port {dns_port} is in use. Trying alternative ports...")
            # Try alternative ports
            for alt_port in range(dns_port + 1, dns_port + 10):
                if self.is_udp_free(self.dns_default_host, alt_port):
                    dns_port = alt_port
                    print(f"Using alternative DNS port: {dns_port}")
                    # Update config
                    self.config_dict["dns_port"] = dns_port
                    try:
                        with open(self.config_file, "w") as f:
                            json.dump(self.config_dict, f, indent=2)
                    except Exception as e:
                        print(f"Error updating config: {e}")
                    break
            else:
                print("No available DNS ports found. Exiting...")
                exit(1)

        self.dns_instance = DNSWrapper(self.dns_default_host, dns_port)

        # Run each proxy from config
        for proxy_data in self.config_dict.get("proxy_instances", {}).values():
            upstream_host = proxy_data.get("upstream_host")
            upstream_port = proxy_data.get("upstream_port")
            username = proxy_data.get("username")
            password = proxy_data.get("password")
            listen_host = proxy_data.get("listen_host")
            listen_port = proxy_data.get("listen_port")

            if not self.is_udp_free(listen_host, listen_port):
                print(f"Port {listen_port} is in use. Skipping...")
                continue

            try:
                proxy = ProxyInterlayer(upstream_host, upstream_port, username, password, listen_host, listen_port)
                self.port_instances[listen_port] = proxy
            except Exception as e:
                print(f"Error starting proxy on {listen_host}:{listen_port}: {e}")

        # Set loglevel
        loglevel = self.config_dict.get("loglevel")
        if loglevel is not None:
            custom_types.LOGLEVEL = loglevel

    def run_forever(self) -> None:
        """Run the API server indefinitely."""
        # Wait for all instances to initialize
        time.sleep(0.5)
        
        # Add shutdown event handler
        @self.fastapi_app.on_event("shutdown")
        async def shutdown_event():
            """Gracefully shutdown on API server stop."""
            print("API server shutting down, stopping all instances...")
            self.shutdown()
            
        # Run the API server
        uvicorn.run(
            self.fastapi_app, 
            host=self.api_host, 
            port=self.api_port,
            log_level="info"
        )

    def shutdown(self) -> None:
        """Gracefully shut down all components."""
        print("Shutting down all components...")
        
        # Stop all proxy instances
        for instance in list(self.port_instances.values()):
            try:
                instance.stop_server()
            except Exception as e:
                print(f"Error stopping proxy instance: {e}")
        
        # Stop DNS server
        if self.dns_instance:
            try:
                self.dns_instance.stop_server()
            except Exception as e:
                print(f"Error stopping DNS server: {e}")
                
        # Clean up shared memory
        try:
            SharedStorage.cleanup()
        except Exception as e:
            print(f"Error cleaning up shared memory: {e}")
            
        print("Shutdown complete")