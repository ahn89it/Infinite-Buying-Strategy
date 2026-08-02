# 무한매수법 V4.0 자동매매 프로그램 실행 이미지.
# kwcli(키움 REST API 클라이언트)가 Python 3.13 이상을 요구하므로 3.13-slim을 사용합니다.
FROM python:3.13-slim

WORKDIR /app

# 의존성 설치를 먼저 해서, 코드만 바뀌었을 때는 이 레이어가 캐시되게 합니다.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY infinite_buying_v4/ ./infinite_buying_v4/

# 로그가 버퍼링 없이 즉시 컨테이너 stdout으로 나가야 `docker logs`로 실시간 확인이 됩니다.
ENV PYTHONUNBUFFERED=1

# 기본 실행: 프리장/본장 스케줄러를 계속 띄워두는 무인 실행 모드.
# 최초 1회 신규 시작(bootstrap)은 컨테이너 안에서 별도로 아래처럼 수동 실행해야 합니다:
#   docker compose run --rm infinite_buying python -m infinite_buying_v4.bootstrap
CMD ["python", "-m", "infinite_buying_v4.scheduler"]
