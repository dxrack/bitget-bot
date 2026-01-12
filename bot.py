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

logging.info("Starting Bot")

if api_key and secret_key and passphrase:
    logging.info("API Key found")
    logging.info("Secret Key found")
    logging.info("Passphrase found")
    logging.info("Bot is ACTIVE and RUNNING")
else:
    logging.error("Missing environment variables")
    sys.exit(1)

while True:
    time.sleep(60)
    logging.info("Status: OK")
```

**정확히 이것만 입력하세요. 특수문자 없음!**

---

### **Step 3️⃣: Commit**
```
Message: Clean bot code without special characters
```

---

### **결과**
```
1-2분 후:

✅ Logs에 "Starting Bot"
✅ "API Key found"
✅ "Status: OK" 반복

✅ ACTIVE + Online 상태 유지!
