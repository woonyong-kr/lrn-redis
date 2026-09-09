# lrn-redis

TCP로 받은 명령이 메모리의 값을 바꾸고, 서버를 다시 시작한 뒤에도 복구되는 과정을 구현한 Python Key-Value 서버다. RESP2 파싱부터 다섯 자료형, 키 만료, 메모리 퇴출, AOF와 snapshot까지 한 프로세스에서 동작한다.

## 실행

Python 3.12와 uv가 필요하다. `make setup`은 이 저장소의 `.venv`만 준비하며 의존성을 `requirements.lock`으로 고정한다.

```sh
make setup
make demo
# 계속 실행할 서버
make serve
# 다른 터미널: redis-cli -p 6380
# 선택: Docker가 있을 때 53개 명령을 Redis 7.4.9와 비교
make compare
```

서버는 Ctrl-C로 종료한다. demo/test는 자신이 만든 프로세스만 종료한다.

## 입력에서 출력까지

TCP RESP2 → dispatcher → 자료구조·TTL·메모리 제한 → AOF/snapshot → RESP2 응답

String·Hash·List·Set·Sorted Set의 53개 명령을 제공한다. 분할 수신과 pipeline, 입력·출력 버퍼 제한, 유휴 연결·write drain timeout을 갖는다. `make demo`는 임시 데이터와 자체 자식 프로세스로 장바구니 저장, coupon 만료, AOF 재시작 복구, LRU 퇴출을 검증하고 끝난다.

메모리 정책은 `noeviction`, `allkeys-lru`, `allkeys-random`, `volatile-ttl`이다. 메모리 사용량은 Python 객체에 대한 논리적 추정치이며 OS RSS 상한이 아니다. 퇴출은 쓰기 이후 적용하므로 random/volatile 정책은 방금 쓴 키를 선택할 수 있다. 이런 최종 상태도 AOF 재생 후 유지한다.

서버 설정은 `MINI_REDIS_*` 환경변수다. 기본은 loopback:6380(`make serve`), 영속성은 꺼져 있다. 예: `MINI_REDIS_APPENDONLY=true MINI_REDIS_AOF_FSYNC=always make serve`. snapshot은 `MINI_REDIS_RDB_ENABLED=true`, 경로는 `MINI_REDIS_RDB_FILE`, 주기는 `MINI_REDIS_RDB_SAVE_INTERVAL_SECONDS`다. AOF 경로는 `MINI_REDIS_AOF_FILE`이다.

연결 처리는 [`server.py`](server.py), 명령 분기는 [`dispatcher.py`](commands/dispatcher.py), 저장과 복구는 [`persistence.py`](store/persistence.py), 자료형과 메모리 관리는 [`datastore.py`](store/datastore.py)에서 시작하면 된다.

## 검증과 관찰

```sh
make test
```

명령의 정상·오류 응답, 분할 입력, 느린 연결, 손상된 AOF, snapshot 복원, fsync와 퇴출 후 복구를 검사한다. `make demo`에서는 coupon이 만료돼 `null`이 되고, `AOF restart` 단계에서도 장바구니가 남아 있는 것을 볼 수 있다. 마지막에는 메모리 한도를 낮춰 일부 키가 퇴출됐는지 확인한다.

`make compare`는 고정된 Redis 7.4.9 이미지와 각 지원 명령의 대표 응답을 대조한다. 전체 Redis 문법이나 성능의 동등성을 검사하는 명령은 아니다.

## 지원 범위와 한계

복제·Cluster·Sentinel·Pub/Sub·Lua·트랜잭션·RESP3를 제공하지 않는다. `SET NX/XX/GET`, 확장 옵션 등 전체 Redis 명령 문법을 지원하지 않는다. MGET에 다른 자료형을 넣으면 전체 WRONGTYPE 오류를 반환하는 등 Redis와 다른 경계 동작이 있다. 정수 연산은 Python 정수여서 Redis의 signed 64-bit 범위를 그대로 보장하지 않는다.

snapshot의 매직 바이트는 `MINIRDB1\r\n`이며 Redis RDB와 호환되지 않는다. `always`는 정상 응답 전에 해당 AOF 기록의 fsync를 완료한다. `everysec`는 0.1초 maintenance loop에서 주기적으로 fsync하며 이벤트 루프·OS 지연에 따라 손실 창이 1초를 넘을 수 있다. `no`는 OS flush에 맡긴다. fsync의 실제 내구성은 파일시스템·장치에 의존한다. snapshot은 임시 파일 fsync 후 replace하지만 디렉터리 fsync까지 수행하지 않으므로 전원 차단 시 파일명 교체의 내구성을 보장하지 않는다.

손상·잘린 AOF는 원본을 자동 절단하지 않고 시작을 실패시킨다. AOF 우선 복구, 없으면 snapshot 복구다. 복구 시 로그에 기록된 퇴출을 재생하고 마지막에 현재 메모리 한도를 적용한다. 디스크 쓰기 실패는 오류 응답을 내며 메모리 변경까지 자동 rollback하지 않는다. 백업을 보존한 채 파일·원인을 확인해야 한다.

## 출처와 기여

원본 `woonyong-kr/mini-redis`의 `05f382d42a3c8ac298de9f8f4dd763c6e97650d4`에서 이어 받은 학습용 파생본이다. 원본 과제·팀 코드와 이후 개인 확장은 Git author와 diff로 구분하며, 기존 저작권 표시는 소스에 유지한다. 원본 주소의 공개 접근이 제한돼 있어 자료는 아래 이력 링크로 확인할 수 있다.

이 파생본에서는 독립 실행 경로와 함께 느린 연결, 저장 실패, 퇴출 이후 복구를 다루는 코드를 보완했다. 이전 설계 문서와 실험은 [정리 전 이력](https://github.com/woonyong-kr/lrn-redis/tree/4ade14e1ec3ec2072d1ae9b7940946652bfb905e)에 남아 있다.
