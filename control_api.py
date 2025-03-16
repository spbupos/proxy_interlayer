import time
import fastapi
import uvicorn
import json
import socket

from interlayer import ProxyInterlayer
from dns_server import DNSWrapper
from shared_storage import SharedStorage
import custom_types



class APIController:
    def __init__(self, config_file="proxy.json", api_host="0.0.0.0", api_port=1984):
        self.fastapi_app = fastapi.FastAPI()
        self.config_file = config_file

        self.config_dict = {}
        self.port_instances = {}
        self.dns_instance = None

        self.dns_default_host = "0.0.0.0"
        self.dns_default_port = 1053
        self.proxy_default_host = "0.0.0.0"

        self.api_host = api_host
        self.api_port = api_port

        self.init_api()
        self.init_methods()


    @staticmethod
    def is_udp_free(host, port):
        """Check if a UDP port is available on the given host."""
        try:
            udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_socket.bind((host, port))
            udp_socket.close()
            return True  # Port is free
        except OSError:
            return False  # Port is in use


    def add_instance_to_dict(self, upstream_host, upstream_port, username, password, listen_host, listen_port):
        self.config_dict['proxy_instances'][str(listen_port)] = {
            "upstream_host": upstream_host,
            "upstream_port": upstream_port,
            "username": username,
            "password": password,
            "listen_host": listen_host,
            "listen_port": listen_port
        }
        try:
            with open(self.config_file, "w") as f:
                json.dump(self.config_dict, f)
        except Exception as e:
            print(f"Error writing config: {e}")


    def remove_instance_from_dict(self, listen_port):
        self.config_dict['proxy_instances'].pop(str(listen_port))
        try:
            with open(self.config_file, "w") as f:
                json.dump(self.config_dict, f)
        except Exception as e:
            print(f"Error writing config: {e}")


    def init_methods(self):
        # POST /start_instance
        # receives JSON with following fields:
        # - upstream_host: str
        # - upstream_port: int
        # - username: str
        # - password: str
        # - listen_port: int
        # - listen_host: int (optional, default is 0.0.0.0)
        @self.fastapi_app.post("/start_instance")
        async def start_instance(request: fastapi.Request):
            data = await request.json()
            upstream_host = data.get("upstream_host")
            upstream_port = int(data.get("upstream_port"))
            username = data.get("username")
            password = data.get("password")
            listen_host = data.get("listen_host")
            listen_port = int(data.get("listen_port"))

            if not upstream_host or not upstream_port or not username or not password or not listen_host or not listen_port:
                return fastapi.Response(content=json.dumps({"error": "Missing fields"}), status_code=400)

            # IMPORTANT: we should check if port is free
            if not self.is_udp_free(listen_host, listen_port):
                return fastapi.Response(content=json.dumps({"error": "Port is in use"}), status_code=400)

            # initializing of interlayer runs new thread
            try:
                proxy = ProxyInterlayer(upstream_host, upstream_port, username, password, listen_host, listen_port)
                self.port_instances[listen_port] = proxy
                self.add_instance_to_dict(upstream_host, upstream_port, username, password, listen_host, listen_port)
                return fastapi.Response(content=json.dumps({"status": f"Instance started on {listen_host}:{listen_port}"}), status_code=200)
            except Exception as e:
                return fastapi.Response(content=json.dumps({"error": f"Error occured: {e}"}), status_code=500)


        # POST /stop_instance
        # receives JSON with following fields:
        # - listen_port: int
        # - listen_host: str (optional, default is 0.0.0.0)
        @self.fastapi_app.post("/stop_instance")
        async def stop_instance(request: fastapi.Request):
            data = await request.json()
            listen_host = data.get("listen_host")
            listen_port = int(data.get("listen_port"))
            print(f'DEBUG: instances={self.port_instances} port={listen_port}')

            if not listen_port:
                return fastapi.Response(content=json.dumps({"error": "Missing fields"}), status_code=400)

            if not listen_host:
                listen_host = self.proxy_default_host

            if listen_port not in self.port_instances:
                return fastapi.Response(content=json.dumps({"error": "Instance not found"}), status_code=404)

            if self.port_instances[listen_port].stopped:
                return fastapi.Response(content=json.dumps({"error": "Instance already stopped"}), status_code=400)

            self.port_instances[listen_port].stop_server()
            self.port_instances.pop(listen_port)
            self.remove_instance_from_dict(listen_port)
            return fastapi.Response(content=json.dumps({"status": f"Instance stopped on {listen_host}:{listen_port}"}), status_code=200)


    def init_api(self):
        # read config file
        try:
            with open(self.config_file, "r") as f:
                self.config_dict = json.load(f)
        except FileNotFoundError:
            print("Config file not found. Creating new...")
            self.config_dict = {"loglevel": 2, "proxy_instances": {}, "dns_port": 1053}
            try:
                with open(self.config_file, "w") as f:
                    json.dump(self.config_dict, f)
            except Exception as e:
                print(f"Can't init config file: {e}")
        except Exception as e:
            print(f"Error reading config: {e}")
            exit(1)

        # init shared storage
        SharedStorage.init()

        # start DNS server
        dns_port = self.config_dict.get("dns_port")
        if not dns_port:
            dns_port = self.dns_default_port

        if not self.is_udp_free(self.dns_default_host, dns_port):
            print("No resources to start DNS server! Exiting...")
            exit(1)

        self.dns_instance = DNSWrapper(self.dns_default_host, dns_port)

        # run each proxy
        for proxy_data in self.config_dict.get("proxy_instances").values():
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

        # set loglevel
        loglevel = self.config_dict.get("loglevel")
        if loglevel:
            custom_types.LOGLEVEL = loglevel


    def run_forever(self):
        time.sleep(0.5) # wait for all instances to start
        uvicorn.run(self.fastapi_app, host=self.api_host, port=self.api_port)


    def shutdown(self):
        if self.dns_instance:
            self.dns_instance.stop_server()
        for instance in self.port_instances.values():
            instance.stop_server()
