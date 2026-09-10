# 🗄️ lrn-redis

TCP 명령으로 값을 저장하고 재시작 후 복구하는 Python Key-Value 서버입니다. RESP2 파싱, 다섯 자료형, TTL, 메모리 퇴출, AOF와 자체 snapshot을 구현합니다.

[Redis Wiki](https://docs.woonyong.com/wiki/redis/) · [서버 진입점](server.py)

## 실행

Python 3.12와 [uv](https://docs.astral.sh/uv/getting-started/installation/)가 필요합니다. 의존성은 `requirements.lock`으로 고정합니다.

```sh
make setup
make demo
make test
# 계속 실행할 서버와 별도 터미널의 클라이언트
make serve
redis-cli -p 6380
# 선택: Docker의 Redis 7.4.9와 지원 명령의 대표 응답 비교
make compare
```

데모는 장바구니 저장·coupon 만료·AOF 재시작 복구·LRU 퇴출을 보여 줍니다. 서버는 Ctrl-C로 종료하며 demo/test는 자신이 만든 프로세스만 종료합니다.

## 구현과 설계

TCP RESP2 → 명령 분기 → 자료구조·TTL·메모리 제한 → 저장 → RESP2 응답으로 이어집니다.

- [server.py](server.py): 분할 수신·pipeline, 입력·출력 버퍼 제한과 느린 연결 timeout.
- [dispatcher.py](commands/dispatcher.py)·[datastore.py](store/datastore.py): String·Hash·List·Set·Sorted Set의 53개 명령. 메모리 정책은 `noeviction`, `allkeys-lru`, `allkeys-random`, `volatile-ttl`입니다.
- [persistence.py](store/persistence.py): AOF 기록·재생과 snapshot 저장·복원. 퇴출된 최종 상태도 재시작 후 유지합니다.

메모리는 Python 객체의 논리적 추정치이며 OS RSS 상한은 아닙니다. `make test`는 명령 오류·분할 입력·느린 연결·손상된 AOF·fsync·퇴출 후 복구를 확인합니다. `make compare`는 대표 응답 대조이며 전체 Redis 문법·성능 적합성 검사가 아닙니다.

## 저장 설정과 보장

기본 `make serve`는 loopback:6380에서 영속성을 끈 상태로 시작합니다.

```sh
MINI_REDIS_APPENDONLY=true MINI_REDIS_AOF_FSYNC=always make serve
```

AOF 경로는 `MINI_REDIS_AOF_FILE`입니다. snapshot은 `MINI_REDIS_RDB_ENABLED=true`, `MINI_REDIS_RDB_FILE`, `MINI_REDIS_RDB_SAVE_INTERVAL_SECONDS`로 설정합니다.

- `always`: 정상 응답 전에 AOF fsync를 완료합니다. `everysec`는 주기적으로 fsync하나 이벤트 루프·OS 지연에 따라 손실 창이 1초를 넘을 수 있습니다. `no`는 OS flush에 맡깁니다.
- snapshot은 `MINIRDB1` 자체 형식으로 Redis RDB와 호환되지 않습니다. 임시 파일 fsync 후 교체하지만 디렉터리 fsync는 하지 않아 전원 차단 시 파일명 교체의 내구성을 보장하지 않습니다.
- AOF가 있으면 우선 복구하고 없으면 snapshot을 사용합니다. 손상·잘린 AOF는 자동 절단하지 않고 시작을 실패시킵니다. 디스크 쓰기 실패 시 오류를 반환하며 이미 바뀐 메모리까지 rollback하지는 않습니다.

## 현재 범위

단일 노드 학습용 서버입니다. 복제·Cluster·Pub/Sub·Lua·트랜잭션·RESP3와 전체 Redis 옵션 문법은 제공하지 않습니다. 다른 자료형을 포함한 MGET의 WRONGTYPE 처리와 Python 정수 범위 등 Redis와 다른 경계 동작이 있습니다.

## 출처와 기여

원본 `woonyong-kr/mini-redis`의 `05f382d42a3c8ac298de9f8f4dd763c6e97650d4`에서 이어 받은 학습용 파생본이다. 원본 과제·팀 코드와 이후 개인 확장은 Git author와 diff로 구분하며, 기존 저작권 표시는 소스에 유지한다. 원본 주소의 공개 접근이 제한돼 있어 자료는 아래 이력 링크로 확인할 수 있다.

이 파생본에서는 독립 실행 경로와 함께 느린 연결, 저장 실패, 퇴출 이후 복구를 다루는 코드를 보완했다. 이전 설계 문서와 실험은 [정리 전 이력](https://github.com/woonyong-kr/lrn-redis/tree/4ade14e1ec3ec2072d1ae9b7940946652bfb905e)에 남아 있다.
