# ==============================================================================
#  mini-redis Makefile
#  Mac(zsh) / Windows(Git Bash / WSL) 동일 명령어 지원
#
#  사용법:
#    make run       ← 전부 자동 실행 (venv 설정 + Docker 정리 + 서비스 시작)
#    make down      ← 실행 중인 컨테이너 전부 종료
#    make logs      ← 전체 로그 스트리밍
#    make ps        ← 컨테이너 상태 확인
#    make clean     ← venv + 결과 파일 모두 삭제
#    make help      ← 이 도움말 출력
# ==============================================================================

-include .env
export

# ── OS 감지 ──────────────────────────────────────────────────────────────────
ifeq ($(OS),Windows_NT)
    DETECTED_OS  := Windows
    PYTHON       := python
    VENV_BIN     := venv\Scripts
    VENV_PYTHON  := venv\Scripts\python.exe
    VENV_PIP     := venv\Scripts\pip.exe
    RM_VENV      := if exist venv rmdir /S /Q venv
    RM_RESULTS   := if exist benchmark\results rmdir /S /Q benchmark\results
    MKDIR_RESULTS:= if not exist benchmark\results mkdir benchmark\results
    DEVNULL      := NUL
else
    DETECTED_OS  := $(shell uname -s)
    PYTHON       := python3
    VENV_BIN     := venv/bin
    VENV_PYTHON  := venv/bin/python
    VENV_PIP     := venv/bin/pip
    RM_VENV      := rm -rf venv
    RM_RESULTS   := rm -rf benchmark/results/*
    MKDIR_RESULTS:= mkdir -p benchmark/results
    DEVNULL      := /dev/null
endif

COMPOSE         := docker compose
PROJECT_COMPOSE := docker-compose.yml
DEV_COMPOSE     := docker-compose.dev.yml
REDIS_REFERENCE_HOST_PORT ?= 6379
MINI_REDIS_HOST_PORT    ?= 6380

.PHONY: run bench down dev dev-down cli cli-official setup-venv install clean logs ps help
.DEFAULT_GOAL  := help

# ══════════════════════════════════════════════════════════════════════════════
#  run ← 모든 것을 한 번에 실행하는 핵심 명령어
#
#  실행 순서:
#    1. 이 프로젝트의 기존 컨테이너 종료
#    2. results 디렉토리 준비
#    3. 기준 Redis, mini-redis, 검증 봇 빌드 및 실행
# ══════════════════════════════════════════════════════════════════════════════
run: down _results-dir
	@echo ""
	@echo "══════════════════════════════════════════"
	@echo "  ▶  Docker 빌드 & 전체 서비스 실행"
	@echo "══════════════════════════════════════════"
	$(COMPOSE) -f $(PROJECT_COMPOSE) up --build --abort-on-container-exit --exit-code-from benchmark
	@echo ""
	@echo "✔  벤치마크 완료 — 결과 확인:"
	@echo "   benchmark/results/compatibility.json"
	@echo "   benchmark/results/performance.json"
	@echo "   benchmark/results/performance.csv"

# ── 벤치마크만 재실행 (이미 서비스가 올라와 있을 때) ────────────────────────
bench:
	@echo "▶ 벤치마크 봇만 재실행..."
	$(COMPOSE) -f $(PROJECT_COMPOSE) run --rm benchmark

# ══════════════════════════════════════════════════════════════════════════════
#  run-demo — 발표용 벤치마크 (5분 이내 완료 보장)
#
#  .env 파일은 건드리지 않고 환경변수를 인라인으로 오버라이드
#
#  53개 명령 차등 검증 후 500회 축소 성능 비교를 실행합니다.
# ══════════════════════════════════════════════════════════════════════════════
run-demo: down _results-dir
	@echo ""
	@echo "══════════════════════════════════════════"
	@echo "  ▶  발표용 벤치마크 실행 (~1-2분 소요)"
	@echo "══════════════════════════════════════════"
	BENCH_ITERATIONS=500 \
	BENCH_ROUNDS=3 \
	BENCH_WARMUP=50 \
	BENCH_PIPELINE_BATCH=50 \
	BENCH_LEADERBOARD_PLAYERS=300 \
	BENCH_KEY_COUNT=100 \
	$(COMPOSE) -f $(PROJECT_COMPOSE) up --build --abort-on-container-exit --exit-code-from benchmark
	@echo ""
	@echo "✔  벤치마크 완료 — 결과 확인:"
	@echo "   benchmark/results/compatibility.json"
	@echo "   benchmark/results/performance.json"
	@echo "   benchmark/results/performance.csv"

# ══════════════════════════════════════════════════════════════════════════════
#  dev — mini-redis 단독 실행 (데모 / 발표 / 수동 테스트용)
#
#  docker-compose.dev.yml 을 사용, 백그라운드(-d)로 기동
#  벤치마크 봇 없음 — 직접 CLI로 명령어 입력
#
#  실행 : make dev
#  접속 : make cli
#  종료 : make dev-down
# ══════════════════════════════════════════════════════════════════════════════
dev:
	@echo ""
	@echo "══════════════════════════════════════════"
	@echo "  ▶  mini-redis 데모 환경 시작"
	@echo "══════════════════════════════════════════"
	$(COMPOSE) -f $(DEV_COMPOSE) up --build -d
	@echo ""
	@echo "✔  mini-redis-dev 실행 중 (포트 $(MINI_REDIS_HOST_PORT))"
	@echo "   접속 : make cli"
	@echo "   종료 : make dev-down"
	@echo ""

# ── dev 환경 종료 ─────────────────────────────────────────────────────────────
dev-down:
	@echo "▶ dev 환경 종료..."
	$(COMPOSE) -f $(DEV_COMPOSE) down --remove-orphans
	@echo "✔ 완료"

# ══════════════════════════════════════════════════════════════════════════════
#  cli — 실행 중인 mini-redis-dev 컨테이너에 redis-cli 접속
#
#  docker run --network container:<name> 으로 컨테이너 네트워크에 직접 진입
#  → 포트 포워딩 없이 내부 6379 포트로 바로 접속
#
#  전제 : make dev 로 mini-redis-dev 컨테이너가 먼저 실행되어 있어야 함
# ══════════════════════════════════════════════════════════════════════════════
cli:
	@echo "▶ mini-redis CLI 접속 중... (컨테이너: mini-redis-dev)"
	@docker inspect mini-redis-dev > $(DEVNULL) 2>&1 || \
		(echo "  ✗ mini-redis-dev 가 실행되어 있지 않습니다. 먼저 'make dev' 를 실행하세요." && exit 1)
	docker run --rm -it \
		--network container:mini-redis-dev \
		redis:7.4.9-alpine@sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99 \
		redis-cli -p 6379

# ══════════════════════════════════════════════════════════════════════════════
#  cli-official — 벤치마크 환경의 공식 Redis 에 redis-cli 접속
#
#  전제 : make run (또는 docker compose up) 으로 redis-reference 가 실행 중이어야 함
# ══════════════════════════════════════════════════════════════════════════════
cli-official:
	@echo "▶ 기준 Redis CLI 접속 중... (컨테이너: redis-reference)"
	@docker inspect redis-reference > $(DEVNULL) 2>&1 || \
		(echo "  ✗ redis-reference 가 실행되어 있지 않습니다. 먼저 'make run' 을 실행하세요." && exit 1)
	docker run --rm -it \
		--network container:redis-reference \
		redis:7.4.9-alpine@sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99 \
		redis-cli -p 6379

# ══════════════════════════════════════════════════════════════════════════════
#  setup-venv — Python 가상환경 확인 / 생성 / 패키지 설치
# ══════════════════════════════════════════════════════════════════════════════
setup-venv:
	@echo ""
	@echo "── [1/4] 환경 감지 ──────────────────────────"
	@echo "   OS     : $(DETECTED_OS)"
	@echo "   Python : $(shell $(PYTHON) --version 2>&1)"
	@echo ""
	@echo "── [2/4] Python 가상환경 확인 ───────────────"
	@if [ ! -f "$(VENV_PYTHON)" ]; then \
		echo "   venv 없음 → 생성 중..."; \
		$(PYTHON) -m venv venv; \
		echo "   ✔ venv 생성 완료"; \
	else \
		echo "   ✔ venv 이미 존재 ($(VENV_PYTHON))"; \
	fi
	@if ! $(VENV_PYTHON) -m pip --version > $(DEVNULL) 2>&1; then \
		echo "   pip 없음 → ensurepip 실행 중..."; \
		$(VENV_PYTHON) -m ensurepip --upgrade; \
	fi
	@echo ""
	@echo "── [3/4] 패키지 설치 (venv) ─────────────────"
	$(VENV_PYTHON) -m pip install --quiet --upgrade pip
	$(VENV_PYTHON) -m pip install --quiet -r requirements.txt
	@echo "   ✔ 패키지 설치 완료"

# ── 패키지만 재설치 ──────────────────────────────────────────────────────────
install: setup-venv

# ══════════════════════════════════════════════════════════════════════════════
#  down — 이 프로젝트가 만든 컨테이너만 종료
# ══════════════════════════════════════════════════════════════════════════════
down:
	@echo ""
	@echo "── 기존 프로젝트 컨테이너 정리 ──────────────"
	@$(COMPOSE) -f $(PROJECT_COMPOSE) down --remove-orphans 2>$(DEVNULL) || true
	@echo "   ✔ 프로젝트 컨테이너 정리 완료"

# ── results 디렉토리 보장 ────────────────────────────────────────────────────
_results-dir:
	@$(MKDIR_RESULTS)

# ══════════════════════════════════════════════════════════════════════════════
#  clean — venv / 결과 파일 / Docker 이미지 전부 삭제
# ══════════════════════════════════════════════════════════════════════════════
clean: down
	@echo "▶ 정리 중..."
	$(RM_VENV)
	$(RM_RESULTS)
	$(COMPOSE) -f $(PROJECT_COMPOSE) down --rmi local --volumes 2>$(DEVNULL) || true
	@echo "✔ 완료"

# ── 유틸리티 ─────────────────────────────────────────────────────────────────

logs:
	$(COMPOSE) -f $(PROJECT_COMPOSE) logs -f

ps:
	$(COMPOSE) -f $(PROJECT_COMPOSE) ps

# ══════════════════════════════════════════════════════════════════════════════
#  help
# ══════════════════════════════════════════════════════════════════════════════
help:
	@echo ""
	@echo "  mini-redis — 사용 가능한 명령어"
	@echo "  ══════════════════════════════════════════════════════"
	@echo ""
	@echo "  ── 데모 / 발표 환경 (docker-compose.dev.yml) ──────"
	@echo "  make dev          mini-redis 단독 컨테이너 백그라운드 실행"
	@echo "  make cli          dev mini-redis 에 redis-cli 접속"
	@echo "  make dev-down     dev 환경 종료"
	@echo ""
	@echo "  ── 벤치마크 환경 (docker-compose.yml) ─────────────"
	@echo "  make run-demo     발표용 벤치마크 (~1-2분, 5분 이내 보장)"
	@echo "  make run          53개 명령 검증 + 전체 성능 비교"
	@echo "  make bench        서비스가 떠 있을 때 벤치마크 봇만 재실행"
	@echo "  make cli-official 공식 Redis 에 redis-cli 접속"
	@echo "  make down         벤치마크 컨테이너 전부 종료"
	@echo "  make logs         전체 로그 스트리밍"
	@echo "  make ps           컨테이너 상태 확인"
	@echo ""
	@echo "  ── 공통 ────────────────────────────────────────────"
	@echo "  make install      Python venv 생성 + 패키지 설치"
	@echo "  make clean        venv + 결과 파일 + Docker 이미지 전부 삭제"
	@echo "  make help         이 도움말 출력"
	@echo ""
	@echo "  감지된 OS : $(DETECTED_OS)"
	@echo ""

# Standalone learning entrypoints; do not start/stop shared Docker services.
.PHONY: setup demo test serve
setup:
	uv venv --python 3.12 .venv --allow-existing
	uv pip sync --python .venv/bin/python requirements.lock

demo:
	.venv/bin/python scripts/demo.py

test:
	.venv/bin/python -m pytest -q

serve:
	.venv/bin/python server.py --port 6380

.PHONY: compare
compare:
	.venv/bin/python scripts/compare.py
