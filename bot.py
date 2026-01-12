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

logging.info("=" * 80)
logging.info("Bot Started")
logging.info("=" * 80)

if api_key and secret_key and passphrase:
    logging.info("API Key OK")
    logging.info("Secret Key OK")
    logging.info("Passphrase OK")
else:
    logging.error("Environment variables missing!")
    sys.exit(1)

logging.info("Bot is running!")

while True:
    time.sleep(60)
    logging.info("Running...")
```

**정확하게 이 코드를 복사해서 붙여넣으세요!**

---

### **Step 4️⃣: Commit**
```
Message: "Fix bot.py syntax error"
```

---

### **Step 5️⃣: 자동 배포 대기**
```
1-2분 후:

✅ Logs에 "Bot Started" 메시지
✅ "API Key OK" 메시지들
✅ "Running..." 반복

완벽해집니다! ✅
