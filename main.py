#!/usr/bin/env python3
"""
Main entry point for the proxy control server.

This script starts the API controller and handles signals
for graceful shutdown.
"""
import argparse
import signal
import sys
import logging
import time
from typing import Optional

from control_api import APIController

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('proxy_server.log')
    ]
)

logger = logging.getLogger("proxy_server")

# Global reference to API controller for signal handlers
api_controller: Optional[APIController] = None

def signal_handler(sig, frame):
    """Handle shutdown signals gracefully."""
    logger.info(f"Received signal {sig}, shutting down...")
    if api_controller:
        api_controller.shutdown()
    sys.exit(0)

def main():
    """Main entry point."""
    global api_controller
    
    parser = argparse.ArgumentParser(description="API controller for proxy instances")
    parser.add_argument("-a", "--api-host", type=str, help="API host")
    parser.add_argument("-p", "--api-port", type=int, help="API port")
    parser.add_argument("-c", "--config-file", type=str, help="Config file")
    parser.add_argument("-l", "--log-level", type=int, choices=[0, 1, 2, 3], 
                        help="Log level (0-3, where 3 is most verbose)")
    args = parser.parse_args()

    # Set default values
    default_api_host = "0.0.0.0"
    default_api_port = 1984
    default_config_file = "proxy.json"

    api_host = args.api_host if args.api_host else default_api_host
    api_port = args.api_port if args.api_port else default_api_port
    config_file = args.config_file if args.config_file else default_config_file

    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        # Start API controller
        logger.info(f"Starting API controller on {api_host}:{api_port}")
        api_controller = APIController(config_file, api_host, api_port)
        
        # Set log level if provided
        if args.log_level is not None:
            import custom_types
            custom_types.LOGLEVEL = args.log_level
            logger.info(f"Log level set to {args.log_level}")
            
        # Run the API server
        api_controller.run_forever()
    except Exception as e:
        logger.error(f"Error in main: {e}", exc_info=True)
    finally:
        # Ensure cleanup on exit
        if api_controller:
            api_controller.shutdown()
        logger.info("Server shutdown complete")


if __name__ == "__main__":
    main()