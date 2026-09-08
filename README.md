# lrn-redis

영속성을 갖춘 단일 노드 Key-Value 서버. 핵심 엔진을 실제 입력으로 실행하고 결과와 내부 동작을 확인하는 독립 프로그램이다.

## 실행

Python 3.12와 uv가 필요하다. `make setup`은 이 저장소의 `.venv`만 준비하며 의존성을 `requirements.lock`으로 고정한다.

```sh
make setup
make test
make demo
# 계속 실행할 서버
make serve
# 다른 터미널: redis-cli -p 6380
# 선택: Docker가 있을 때 53개 명령을 Redis 7.4.9와 비교
make compare
```

대화형 서버는 해당 터미널에서 Ctrl-C로 종료한다. demo/test의 자식 프로세스는 실행기가 보유한 PID 또는 컨테이너 ID로만 종료한다. 다른 서버를 포트 번호로 찾아 일괄 종료하지 않는다. 준비된 Python 환경이 없으면 먼저 `make setup`을 실행한다.

## 입력에서 출력까지

TCP RESP2 → dispatcher → 자료구조·TTL·메모리 제한 → AOF/snapshot → RESP2 응답

String·Hash·List·Set·Sorted Set의 53개 명령을 제공한다. 분할 수신과 pipeline, 입력·출력 버퍼 제한, 유휴 연결·write drain timeout을 갖는다. `make demo`는 임시 데이터와 자체 자식 프로세스로 장바구니 저장, coupon 만료, AOF 재시작 복구, LRU 퇴출을 검증하고 끝난다.

메모리 정책은 `noeviction`, `allkeys-lru`, `allkeys-random`, `volatile-ttl`이다. 메모리 사용량은 Python 객체에 대한 논리적 추정치이며 OS RSS 상한이 아니다. 퇴출은 쓰기 이후 적용하므로 random/volatile 정책은 방금 쓴 키를 선택할 수 있다. 이런 최종 상태도 AOF 재생 후 유지한다.

서버 설정은 `MINI_REDIS_*` 환경변수다. 기본은 loopback:6380(`make serve`), 영속성은 꺼져 있다. 예: `MINI_REDIS_APPENDONLY=true MINI_REDIS_AOF_FSYNC=always make serve`. snapshot은 `MINI_REDIS_RDB_ENABLED=true`, 경로는 `MINI_REDIS_RDB_FILE`, 주기는 `MINI_REDIS_RDB_SAVE_INTERVAL_SECONDS`다. AOF 경로는 `MINI_REDIS_AOF_FILE`이다.

구현을 읽는 순서: `server.py`, `commands/dispatcher.py`, `store/persistence.py`, `store/datastore.py`.

## 검증과 관찰

154개 Python 테스트, Redis 7.4.9의 53개 대표 정상 명령과 차등 비교. 분할 입력·잘못된 인수·느린 TCP client·AOF 손상·snapshot 다중 자료형·fsync·퇴출 후 복구를 포함한다. 차등 비교는 전체 문법 호환성 검증이 아니다.

실행 환경·명령·exit code·원본 백업과 전체 결과는 이번 전환의 별도 작업 폴더에 기록한다. 새 기계에서는 같은 명령으로 직접 재검증한다. 수치가 기록되어 있다는 사실과 현재 실행 성공을 구분한다.

## 지원 범위와 한계

복제·Cluster·Sentinel·Pub/Sub·Lua·트랜잭션·RESP3를 제공하지 않는다. `SET NX/XX/GET`, 확장 옵션 등 전체 Redis 명령 문법을 지원하지 않는다. MGET에 다른 자료형을 넣으면 전체 WRONGTYPE 오류를 반환하는 등 Redis와 다른 경계 동작이 있다. 정수 연산은 Python 정수여서 Redis의 signed 64-bit 범위를 그대로 보장하지 않는다.

snapshot의 매직 바이트는 `MINIRDB1\r\n`이며 Redis RDB와 호환되지 않는다. `always`는 정상 응답 전에 해당 AOF 기록의 fsync를 완료한다. `everysec`는 0.1초 maintenance loop에서 주기적으로 fsync하며 이벤트 루프·OS 지연에 따라 손실 창이 1초를 넘을 수 있다. `no`는 OS flush에 맡긴다. fsync의 실제 내구성은 파일시스템·장치에 의존한다. snapshot은 임시 파일 fsync 후 replace하지만 디렉터리 fsync까지 수행하지 않으므로 전원 차단 시 파일명 교체의 내구성을 보장하지 않는다.

손상·잘린 AOF는 원본을 자동 절단하지 않고 시작을 실패시킨다. AOF 우선 복구, 없으면 snapshot 복구다. 복구 시 로그에 기록된 퇴출을 재생하고 마지막에 현재 메모리 한도를 적용한다. 디스크 쓰기 실패는 오류 응답을 내며 메모리 변경까지 자동 rollback하지 않는다. 백업을 보존한 채 파일·원인을 확인해야 한다.

## 원본·학습 문서의 경계

[원본 아카이브와 기여 구분](archive/README.md)을 확인한다. 이 저장소는 실행 코드·테스트·사용법·설계 근거를 소유한다. WIKI는 개념 정본을 소유하며 기존 정본·공통 색인·배포 파일을 이 작업에서 수정하지 않는다. SQL·PintOS와 RepoLM/음성 서비스는 이 프로그램의 실행 의존성이 아니다.
