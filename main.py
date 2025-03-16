import argparse
from control_api import APIController

def main():
    parser = argparse.ArgumentParser(description="API controller for proxy instances")
    parser.add_argument("-a", "--api-host", type=str, help="API host")
    parser.add_argument("-p", "--api-port", type=int, help="API port")
    parser.add_argument("-c", "--config-file", type=str, help="Config file")
    args = parser.parse_args()

    default_api_host = "0.0.0.0"
    default_api_port = 1984
    default_config_file = "proxy.json"

    api_host = args.api_host if args.api_host else default_api_host
    api_port = args.api_port if args.api_port else default_api_port
    config_file = args.config_file if args.config_file else default_config_file

    a = APIController(config_file, api_host, api_port)
    a.run_forever()

    a.shutdown()


if __name__ == "__main__":
    main()
