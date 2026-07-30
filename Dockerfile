FROM python:3.11.15-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

WORKDIR /app

# 의존성 먼저 복사 (레이어 캐싱 최적화)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 코드 복사
COPY . .

EXPOSE 6379

CMD ["python", "-u", "server.py"]
