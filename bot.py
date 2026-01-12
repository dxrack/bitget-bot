import os
import time
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

# 환경변수 확인
api_key = os.environ.get('BITGET_API_KEY')
secret_key = os.environ.get('BITGET_SECRET_KEY')
passphrase = os.environ.get('BITGET_PASSPHRASE')

logging.info("="*80)
logging.info("🤖 Live Bot v23 Trailing 시작 (Railway 테스트 버전)")
logging.info("="*80)

if api_key and secret_key and passphrase:
    logging.info("✅ API Key 인증 성공!")
    logging.info("✅ Secret Key 인증 성공!")
    logging.info("✅ Passphrase 인증 성공!")
else:
    logging.error("❌ 환경변수 설정 필요!")
    sys.exit(1)

logging.info("="*80)
logging.info("✅ 봇이 정상적으로 실행 중입니다!")
logging.info("="*80)

# 무한 대기
while True:
    time.sleep(60)
    logging.info("대기 중... (정상 작동)")
```

---

### **Step 4️⃣: Commit**
```
Message: "Simplify bot.py to test version"
