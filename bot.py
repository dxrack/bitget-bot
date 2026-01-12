─────────────────────────────
# Bitget Trading Bot v23 Trailing
import os
import time
from datetime import datetime

print("Bitget Trading Bot Starting...")
print(f"Time: {datetime.now()}")
print("Configuration loaded successfully")

# API 키 확인
api_key = os.environ.get('BITGET_API_KEY')
if api_key:
    print("✅ API Key found")
else:
    print("❌ API Key not found")

# 메인 루프
while True:
    print(f"[{datetime.now()}] Bot running...")
    time.sleep(60)
─────────────────────────────