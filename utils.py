import hashlib
import socket
import ipaddress
from typing import Optional
from logger_config import logger

def get_local_ip() -> Optional[str]:
    """
    Automatically detects the machine's local IP address.

    Returns:
        Optional[str]: The local IP address, or None if detection fails.
    """
    try:
        # Connect to a remote address to determine the local IP
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            logger.info(f"Auto-detected local IP: {local_ip}")
            return local_ip
    except Exception as e:
        logger.warning(f"Could not auto-detect local IP: {e}")
        return None

def calculate_file_hash(file_path: str, algorithm: str = 'sha256') -> Optional[str]:
    """
    Calculates the hash of a file.

    Args:
        file_path (str): Path to the file
        algorithm (str): Hash algorithm to use (default: sha256)

    Returns:
        Optional[str]: The hash string, or None if calculation fails
    """
    try:
        hash_obj = hashlib.new(algorithm)
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_obj.update(chunk)
        return hash_obj.hexdigest()
    except Exception as e:
        logger.error(f"Failed to calculate hash for {file_path}: {e}")
        return None

def calculate_bytes_hash(data: bytes, algorithm: str = 'sha256') -> Optional[str]:
    """
    Calculates the hash of bytes data.

    Args:
        data (bytes): The data to hash
        algorithm (str): Hash algorithm to use (default: sha256)

    Returns:
        Optional[str]: The hash string, or None if calculation fails
    """
    try:
        hash_obj = hashlib.new(algorithm)
        hash_obj.update(data)
        return hash_obj.hexdigest()
    except Exception as e:
        logger.error(f"Failed to calculate hash for bytes data: {e}")
        return None

def format_file_size(size_bytes: int) -> str:
    """
    Formats file size in bytes to human-readable format.

    Args:
        size_bytes (int): File size in bytes

    Returns:
        str: Formatted file size string
    """
    if size_bytes == 0:
        return "0 B"

    size_names = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size_bytes)

    while size >= 1024.0 and i < len(size_names) - 1:
        size /= 1024.0
        i += 1

    return f"{size:.1f} {size_names[i]}"

def is_valid_ip(ip_string: str) -> bool:
    """
    Validates if a string is a valid IP address.

    Args:
        ip_string (str): The IP address string to validate

    Returns:
        bool: True if valid IP address, False otherwise
    """
    try:
        ipaddress.ip_address(ip_string)
        return True
    except ValueError:
        return False

def is_valid_domain(domain: str) -> bool:
    """
    Basic validation for domain names.

    Args:
        domain (str): The domain to validate

    Returns:
        bool: True if domain appears valid, False otherwise
    """
    if not domain or len(domain) > 255:
        return False

    # Remove protocol if present
    if domain.startswith(('http://', 'https://')):
        domain = domain.split('://', 1)[1]

    # Remove path if present
    domain = domain.split('/')[0]

    # Basic domain validation
    parts = domain.split('.')
    if len(parts) < 2:
        return False

    for part in parts:
        if not part or len(part) > 63:
            return False
        if not part.replace('-', '').isalnum():
            return False
        if part.startswith('-') or part.endswith('-'):
            return False

    return True
