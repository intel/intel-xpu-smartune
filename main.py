import signal
import logging
from config import Config

# from controller.controller import Controller
from balancer.balancer import DynamicBalancer

def main():
    # Set up logging
    # logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    # Load configuration
    config = Config.from_file("config.yaml")

    # Create and run balancer
    balancer = DynamicBalancer(logging, config)

    # Handle graceful shutdown
    def shutdown(signum, frame):
        logging.info("Shutting down gracefully...")
        exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logging.info("Starting Dynamic Workload Balancer")
    balancer.balance()

if __name__ == "__main__":
    main()
