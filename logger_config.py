import logging
import sys

def setup_logging():
    """
    Configures the logging for the application.
    It sets up a logger that outputs messages to the console with a specific format.
    This ensures that all modules use a consistent logging style.
    """
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # Create a handler to write to the console
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)

    # Create a formatter and add it to the handler
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)

    # Add the handler to the logger
    logger.addHandler(handler)

    return logger

# Initialize the logger
logger = setup_logging()
