# Redis 비교 검증

## 무엇을 확인하는가

두 가지 질문을 분리했습니다.

1. 공개한 53개 명령의 대표 성공 경로가 Redis 7.4.9와 같은 결과를 내는가?
2. 동일한 단일 클라이언트 조건에서 자료형별 지연 시간이 얼마나 다른가?

첫 질문은 `benchmark/compatibility.py`, 두 번째 질문은
`benchmark/benchmark.py`가 담당합니다.

## 이전 결과를 폐기한 이유

기존 검증기는 일부 시나리오가 예외를 내면 “미구현”으로 출력하고 계속
실행했습니다. 결과 파일에 없는 시나리오는 오류 수에도 포함되지 않았으므로
“4,020회, 오류 0건”은 전체 성공을 뜻하지 않았습니다.

또한 기준 Redis, mini-redis, MongoDB를 한 보고서에 섞어 Redis 호환
구현의 비교 목적이 흐려졌고, 이미지 버전과 실행 조건도 결과 파일에
충분히 남지 않았습니다. 해당 결과는 삭제하고 사용하지 않습니다.

현재 검증기는 다음 경우 모두 실패 코드로 종료합니다.

- 서비스가 준비되지 않음
- 공개 명령 목록과 차등 사례 목록이 다름
- 기준 Redis 또는 mini-redis 명령이 예외를 반환
- 성공 응답의 값이나 자료형이 다름
- 성능 시나리오 응답 후조건 실패
- 양쪽 중 한 시나리오 결과가 누락

## 고정한 환경

2026-07-31 검증:

| 항목 | 값 |
|---|---|
| 호스트 | Apple Silicon, Docker Desktop |
| 컨테이너 플랫폼 | Linux aarch64 |
| 기준 Redis | 7.4.9 |
| Redis 이미지 | `redis:7.4.9-alpine` + sha256 digest 고정 |
| Python | 3.11.15 이미지 + sha256 digest 고정 |
| 클라이언트 | redis-py 6.4.0 |
| 네트워크 | 동일 Docker bridge network |
| 연결 | 서비스별 단일 연결, 순차 명령 |
| 영속성 | 양쪽 모두 비활성 |
| maxmemory | 양쪽 모두 비활성 |
| 측정 | warmup 50, 시나리오별 500회, 전체 3회 |
| 값/키 | 값 32 bytes, keyspace 100 |
| Sorted Set | 300 members |
| pipeline | batch 50 |

검증 원본:

- [53개 명령 결과](../benchmark/verified/2026-07-31-arm64/compatibility.json)
- [성능 JSON](../benchmark/verified/2026-07-31-arm64/performance.json)
- [성능 CSV](../benchmark/verified/2026-07-31-arm64/performance.csv)

## 차등 검증 결과

53개 공개 명령의 대표 성공 경로가 53/53 일치했습니다.

비교할 때 순서가 계약이 아닌 응답은 정렬했고, TTL은 실행 시각 차이를 고려해
1초 오차를 허용했습니다. 나머지는 redis-py가 해석한 값과 자료형을
비교했습니다.

이 수치가 보장하지 않는 것:

- 명령의 모든 옵션 조합
- 모든 오류 경로와 오류 문자열의 완전 일치
- 동시 실행의 원자성
- Redis 서버의 시간·메모리 복잡도 일치
- RESP3, replication, cluster

따라서 이력서에는 “Redis 완전 호환”이 아니라 다음처럼 쓰는 것이 정확합니다.

> RESP2 기반 인메모리 서버에 53개 명령을 연결하고, 고정한 Redis 7.4.9
> 이미지와 명령별 대표 성공 경로 53개를 차등 검증했습니다.

## 성능 결과

각 행은 세 번 측정한 p95의 중앙값입니다.

| 시나리오 | Redis 7.4.9 | mini-redis | mini / Redis |
|---|---:|---:|---:|
| GET | 0.1222 ms | 0.1349 ms | 1.10× |
| SET | 0.1535 ms | 0.2780 ms | 1.81× |
| HGETALL | 0.2369 ms | 0.3812 ms | 1.61× |
| INCR | 0.1977 ms | 0.4665 ms | 2.36× |
| LPUSH | 0.1726 ms | 0.2979 ms | 1.73× |
| LPOP | 0.1857 ms | 0.2680 ms | 1.44× |
| ZADD + 상위 10개 조회 | 0.3700 ms | 1.5322 ms | 4.14× |
| 개별 SET 50개 | 3.5263 ms | 5.7174 ms | 1.62× |
| pipeline SET 50개 | 0.3163 ms | 0.9997 ms | 3.16× |

## 해석

GET p95 중앙값은 기준 Redis의 1.10배였습니다. 네트워크 왕복이 대부분인
작은 읽기에서는 구현 차이가 상대적으로 작았습니다.

SET, INCR, List 변경은 1.4~2.4배였습니다. 변경 후 Python 객체 크기를
추정하는 비용과 Python 자료구조 조작이 추가됩니다.

Sorted Set 갱신과 상위 10개 조회는 4.14배로 가장 큰 차이를 보였습니다.
점수 갱신 시 dict와 직접 구현한 skiplist를 함께 변경하고, Python 수준에서
노드와 span을 관리하는 경로가 병목임을 보여줍니다.

pipeline은 양쪽 모두 개별 50회 요청보다 빨랐습니다. 다만 mini-redis의
batch p95는 기준 Redis의 3.16배였습니다. RTT 제거만으로 실행 엔진과
자료구조 구현 차이가 사라지지는 않습니다.

## 메모리 갱신 경로 전후 비교

Redis 비교와 별도로, 한 키를 덮어쓸 때 전체 키 공간을 다시 순회하던 문제를
같은 Python 인터프리터에서 비교했습니다.

- 비교 기준: `baf1229`와 현재 구현
- 사전 적재: 동일한 32-byte String을 내부 키 공간에 넣고 전체 계측 1회
- 측정: 첫 번째 키를 100회 덮어쓰기
- 반복: keyspace별 5회, 총 시간의 중앙값

| keyspace | 변경 전 | 변경 후 |
|---:|---:|---:|
| 100 | 48.9801 ms | 0.4826 ms |
| 1,000 | 463.5040 ms | 0.4960 ms |
| 5,000 | 2,332.6597 ms | 0.4828 ms |

변경 전에는 키 수가 10배 늘 때 한 키 변경 시간도 거의 10배 늘었습니다.
변경 후에는 이 실험 범위에서 0.48~0.50 ms로 유지됐습니다. 이는 제품
처리량 수치가 아니라 전체 키 공간 O(N) 재계산을 제거했다는 회귀 근거입니다.

측정기는 `benchmark/memory_scaling.py`입니다. `--module-root`에 비교할
checkout을 넘기면 같은 workload로 다시 실행할 수 있습니다.

```bash
python benchmark/memory_scaling.py --module-root .
```

## 재현

축소 검증:

```bash
make run-demo
```

기본 검증:

```bash
make run
```

출력:

```text
benchmark/results/compatibility.json
benchmark/results/performance.json
benchmark/results/performance.csv
```

결과는 로컬 산출물이므로 기본적으로 Git에서 제외합니다. 검증해 공개할
결과만 `benchmark/verified/<date>-<platform>/`에 옮깁니다.

## 숫자를 사용할 때의 한계

이 측정은 엔진의 상대적 동작을 관찰하기 위한 학습 벤치마크입니다.

- 단일 클라이언트라 동시 접속 한계를 말할 수 없습니다.
- Docker Desktop 스케줄링과 호스트 부하의 영향을 받습니다.
- 세 번의 축소 실행이므로 운영 SLO 근거가 아닙니다.
- Redis는 C와 jemalloc, mini-redis는 Python 객체를 사용합니다.
- durability 비용을 분리하기 위해 영속성을 껐습니다.

따라서 처리량을 제품 용량으로 제시하지 않고, 동일 조건에서 드러난
병목의 위치와 검증기를 고친 과정만 포트폴리오 근거로 사용합니다.
