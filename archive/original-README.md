> 원본 시점의 README 사본. 아래 상대 경로와 명령은 당시 저장소 루트 기준이며 현재 실행법은 루트 README를 따른다.

# mini-redis

Redis를 사용법으로만 익히지 않기 위해 RESP2 서버, 자료구조, TTL, 영속성,
메모리 제한을 Python으로 구현한 학습 프로젝트입니다. `redis-cli`와
`redis-py`가 별도 어댑터 없이 접속할 수 있습니다.

이 저장소가 증명하려는 것은 “Redis를 대체했다”가 아닙니다. 프로토콜 요청이
어떻게 자료구조 변경으로 이어지고, 만료·영속성·메모리 제한 같은 운영 조건이
어디에서 개입하는지 코드로 추적할 수 있다는 점입니다.

## 현재 검증 상태

2026-07-31, Apple Silicon의 Docker Desktop 환경에서 다시 검증했습니다.

| 검증 | 결과 | 범위 |
|---|---:|---|
| 단위·TCP 통합 테스트 | 96 passed | RESP, 53개 명령, 자료구조, TTL, AOF/RDB, 메모리 제한 |
| Redis 7.4.9 차등 검증 | 53/53 일치 | 공개한 각 명령의 대표 성공 경로 1개 |
| 성능 비교 | 9개 시나리오 × 2개 구현 × 3회 | 단일 클라이언트, 순차 요청, 영속성 비활성 |

53/53은 전체 Redis 호환성을 뜻하지 않습니다. 각 명령의 대표 성공 경로를
기준 Redis와 비교한 결과입니다. 오류 문구 전체, 동시성, RESP3, 복제와
클러스터 동작은 검증 범위가 아닙니다.

- [검증 방법과 결과 해석](docs/BENCHMARK.md)
- [코드 실행 흐름](docs/CODE-WALKTHROUGH.md)
- [원본 검증 결과](benchmark/verified/2026-07-31-arm64/)

## 요청이 처리되는 경로

```text
redis-cli / redis-py
        │ TCP + RESP2
        ▼
server.py
  ├─ 입력 버퍼·유휴 시간·tick당 명령 수 제한
  ├─ protocol/parser.py
  ├─ commands/dispatcher.py
  └─ commands/*_cmds.py
             │
             ▼
       store/datastore.py
        ├─ String / Hash / List / Set / Sorted Set
        ├─ TTL: lazy expiry + sampled active expiry
        ├─ AOF / 자체 snapshot
        └─ maxmemory / eviction
             │
             ▼
      protocol/encoder.py
```

`server.py`는 단일 이벤트 루프에서 연결별 코루틴을 실행합니다. 파서는 TCP
스트림에서 완성된 RESP 배열만 꺼내고, 디스패처는 명령 이름을 53개 핸들러 중
하나에 연결합니다. 명령 핸들러는 인자와 타입을 검증한 뒤 저장 계층을
변경합니다.

## 구현 범위

| 영역 | 구현 |
|---|---|
| 프로토콜 | RESP2 Array/Bulk String 요청, Simple String/Error/Integer/Bulk/Array 응답 |
| String | GET, SET, MGET, MSET, INCR, DECR, INCRBY, APPEND, STRLEN |
| Hash | HSET, HGET, HMSET, HMGET, HGETALL, HDEL, HEXISTS, HKEYS, HVALS, HLEN |
| List | LPUSH, RPUSH, LPOP, RPOP, LRANGE, LLEN, LINDEX, LSET |
| Set | SADD, SREM, SMEMBERS, SISMEMBER, SCARD, SINTER, SUNION, SDIFF |
| Sorted Set | ZADD, ZREM, ZSCORE, ZRANK, ZRANGE, ZREVRANGE, ZCARD, ZRANGEBYSCORE |
| Key/TTL | PING, DEL, EXISTS, EXPIRE, TTL, PERSIST, PEXPIREAT, TYPE, KEYS, FLUSHALL |
| 자료구조 | MurmurHash3, separate chaining hash table, open addressing 비교 구현, skiplist |
| 운영 조건 | AOF, 자체 snapshot, lazy/active expiry, 네 가지 eviction 정책 |
| 클라이언트 보호 | idle timeout, 입출력 버퍼 상한, drain timeout, tick당 명령 상한 |

## 실행

### Docker로 서버만 실행

```bash
make dev
make cli
```

`make cli`는 별도 바이너리를 저장소에 포함하지 않고, 고정한 Redis Docker
이미지의 `redis-cli`를 사용합니다.

종료:

```bash
make dev-down
```

### 로컬 실행

Python 3.10 이상을 권장합니다.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python server.py --host 127.0.0.1 --port 6379
```

다른 터미널에서 설치된 `redis-cli`로 접속합니다.

```bash
redis-cli -p 6379
```

```text
127.0.0.1:6379> PING
PONG
127.0.0.1:6379> SET hello world
OK
127.0.0.1:6379> GET hello
"world"
```

## 테스트

```bash
python -m pytest -q
ruff check . --exclude venv --exclude benchmark/results
```

테스트는 명령 반환값뿐 아니라 다음 계약을 확인합니다.

- 분할된 TCP 프레임과 여러 명령이 한 프레임에 들어오는 경우
- 잘못된 자료형과 인자 수
- 음수 인덱스와 범위
- TTL lazy/active 만료
- AOF 재생과 snapshot 복구
- `noeviction`, `allkeys-random`, `allkeys-lru`, `volatile-ttl`
- 느린 클라이언트와 입출력 버퍼 제한
- 한 키 변경이 전체 키 공간 메모리를 다시 계산하지 않는지
- 메모리 제한이 없을 때 변경 전 값을 깊은 복사하지 않는지
- 차등 검증의 53개 명령 목록과 실제 디스패처 목록이 같은지

## 기준 Redis와 비교

```bash
make run-demo  # 53개 명령 검증 + 500회 워크로드 × 3회
make run       # 기본 1,000회 워크로드 × 5회
```

비교 환경은 다음을 고정합니다.

- Redis 7.4.9 이미지와 digest
- 같은 Docker bridge network
- 같은 `redis-py` 클라이언트
- 양쪽 모두 AOF/RDB와 maxmemory 비활성
- 같은 키 수, 값 크기, 요청 순서
- 한 시나리오라도 누락되거나 실패하면 프로세스 실패

축소 검증 3회의 p95 중앙값은 다음과 같습니다.

| 시나리오 | Redis 7.4.9 | mini-redis | 차이 |
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

이 결과에서 읽기 한 건은 비교적 가깝지만, Python 객체 계측과 직접 구현한
skiplist가 개입하는 쓰기·정렬 연산에서 차이가 커지는 것을 확인했습니다.
숫자는 로컬 단일 클라이언트 결과이며 운영 처리량이나 동시 접속 한계를
의미하지 않습니다.

## 메모리 계측을 다시 설계한 이유

초기 구현은 모든 쓰기 전에 변경 전 객체를 `deepcopy`하고, 한 키가 바뀔
때마다 전체 데이터셋의 객체 그래프를 다시 순회했습니다. 데이터가 늘수록
명령 하나의 비용이 전체 키 수에 비례했고, 기존 벤치마크의 쓰기 결과를
왜곡했습니다.

현재 구현은 다음과 같이 바꿨습니다.

- 메모리 제한이 꺼져 있으면 rollback용 깊은 복사를 생략
- 메모리 제한이 켜진 경우에만 변경 중인 키를 복사
- 키별 추정 크기를 보관하고 바뀐 키의 차이만 반영
- 삭제 시 해당 키의 기존 추정 크기만 차감
- TTL 추가·제거도 해당 키의 추정 크기만 갱신
- 전체 재계산은 복구·검증용 명시적 경로로 제한

따라서 일반 쓰기는 전체 키 수가 아니라 변경한 값의 크기에 영향을 받습니다.
이 프로젝트의 메모리 값은 Python 객체 그래프의 근사치이며 Redis의 allocator
기반 `used_memory`와 같은 값은 아닙니다.

## 저장과 복구

AOF는 성공한 쓰기 명령을 RESP 배열로 기록하고, 재시작 시 같은 디스패처를
통해 재생합니다. snapshot 파일은 Redis RDB 포맷이 아니라
`MINIRDB1` 헤더 뒤에 현재 상태를 복원할 명령 스트림을 저장하는 학습용
포맷입니다.

```bash
MINI_REDIS_APPENDONLY=yes \
MINI_REDIS_AOF_FILE=data/appendonly.aof \
python server.py
```

```bash
MINI_REDIS_RDB_ENABLED=yes \
MINI_REDIS_RDB_FILE=data/dump.rdb \
MINI_REDIS_RDB_SAVE_INTERVAL_SECONDS=30 \
python server.py
```

AOF가 존재하면 AOF를 먼저 재생하고, 없을 때 snapshot을 읽습니다.

## 프로젝트에서 의도적으로 제외한 것

- RESP3
- Pub/Sub, Streams
- MULTI/EXEC/WATCH
- Lua scripting
- ACL/AUTH/TLS
- replication, Sentinel, Cluster
- Redis 원본 RDB 파일 호환
- 다중 프로세스·다중 노드 확장
- 운영 환경의 부하·장애 복구 보장

## 저장소를 읽는 순서

1. [protocol/parser.py](protocol/parser.py) — TCP 바이트에서 명령 추출
2. [commands/dispatcher.py](commands/dispatcher.py) — 53개 명령 라우팅
3. [store/datastore.py](store/datastore.py) — 키 공간과 변경 계약
4. [store/hash_table.py](store/hash_table.py) — Hash 자료구조 비교
5. [store/skiplist.py](store/skiplist.py) — Sorted Set 순서와 rank
6. [store/expiry.py](store/expiry.py) — lazy/active expiry
7. [store/persistence.py](store/persistence.py) — AOF/snapshot 기록과 재생
8. [server.py](server.py) — 네트워크 경계와 느린 클라이언트 보호

이 프로젝트는 KRAFTON Jungle의 팀 학습 결과를 바탕으로 합니다. 개인
저장소에서는 기존 구현을 다시 감사해 메모리 갱신 경로와 검증기의 누락을
수정하고, 재현 가능한 학습 아카이브로 정리했습니다.
