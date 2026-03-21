# Ensure the real 'telegram' package is in sys.modules before test collection
# This prevents a Python 3.14 / pytest 9 issue where importing telegram_uploader
# causes telegram to be registered as a non-package namespace.
try:
    import telegram  # noqa: F401
except ImportError:
    telegram = None
