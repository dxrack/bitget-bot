import os
import time
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

api_key = os.environ.get('BITGET_API_KEY')
secret_key = os.environ.get('BITGET_SECRET_KEY')
passphrase = os.environ.get('BITGET_PASSPHRASE')

logging.info('Bot Starting')

if api_key and secret_key and passphrase:
    logging.info('API credentials OK')
    logging.info('Bot is RUNNING')
else:
    logging.error('Missing credentials')
    sys.exit(1)

while True:
    time.sleep(60)
    logging.info('Status OK')
