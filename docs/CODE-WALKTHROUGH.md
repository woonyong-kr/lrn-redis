# 코드 실행 흐름

이 문서는 “명령 하나가 어디를 지나고 어떤 상태를 바꾸는가”를 기준으로
코드를 읽기 위한 안내서입니다.

## 1. TCP 경계

`server.py`의 `Server.handle_client()`가 연결 하나를 담당합니다.

1. `reader.read()`로 받은 바이트를 연결별 버퍼에 누적합니다.
2. 입력 버퍼가 설정값을 넘으면 오류를 보내고 연결을 닫습니다.
3. `protocol.parser.parse()`가 완성된 명령 하나와 소비한 바이트 수를
   반환합니다.
4. 명령을 `commands.dispatcher.dispatch()`로 보냅니다.
5. 성공한 쓰기 결과를 `PersistenceManager.record_command()`에 넘깁니다.
6. `protocol.encoder.encode()`로 RESP 응답을 만들고 전송합니다.

한 번 읽은 TCP 프레임에 명령이 여러 개 들어올 수 있고, 명령 하나가 여러
프레임으로 나뉠 수도 있습니다. 파서는 불완전한 요청이면 소비하지 않고 다음
read를 기다립니다.

한 연결이 이벤트 루프를 독점하지 않도록 처리한 명령 수가
`max_commands_per_tick`에 도달하면 `drain()` 후 제어권을 양보합니다.

## 2. RESP 파서와 인코더

`protocol/parser.py`는 이 서버가 받는 RESP2 Array와 Bulk String을
처리합니다.

```text
*3\r\n
$3\r\nSET\r\n
$3\r\nkey\r\n
$5\r\nvalue\r\n
```

파서 결과는 `["SET", "key", "value"]`입니다. 인코더는 Python 결과를 다음
RESP 타입으로 바꿉니다.

- `SimpleString` → `+OK`
- `RespError` → `-ERR ...`
- `int` → `:1`
- `bytes`·`str` → Bulk String
- `None` → Null Bulk String
- `list`·`set`·`dict` → Array

## 3. 디스패치와 명령 경계

`commands/dispatcher.py`는 모듈별 핸들러를 한 번 조합해
`COMMAND_TABLE`을 만듭니다. 현재 공개 명령은 53개입니다.

핸들러의 책임은 세 가지입니다.

1. 인자 수와 숫자 형식을 검증
2. 현재 키의 Redis 자료형을 검증
3. `DataStore` 메서드를 호출하고 Redis 응답 형태로 변환

`DataStore`가 던진 `MemoryLimitError`는 OOM RESP 오류로 변환됩니다.
그 외 예상하지 못한 예외도 연결을 끊는 대신 RESP 오류로 반환합니다.

## 4. 키 공간과 메모리 계약

`store/datastore.py`는 다음 상태를 함께 소유합니다.

```text
_data         key -> RedisObject
_last_access  key -> monotonic timestamp
_key_sizes    key -> estimated bytes
_used_memory  sum(_key_sizes.values())
```

`RedisObject`는 Redis 논리 타입, 내부 인코딩 이름, 실제 Python 값을
묶습니다.

변경 경로는 다음 순서를 지킵니다.

1. maxmemory가 켜진 경우 변경할 키만 rollback snapshot으로 복사
2. 값을 변경
3. 마지막 접근 시각 갱신
4. 변경한 키의 이전 추정 크기와 현재 추정 크기 차이 반영
5. maxmemory 검사와 필요한 eviction
6. `noeviction` 실패 시 해당 키 snapshot 복원

초기 구현처럼 매 명령마다 전체 데이터셋을 다시 계산하지 않습니다.
`recompute_memory_usage()`는 복구 뒤 검증하거나 전체 상태를 명시적으로 다시
계산할 때만 사용합니다.

`deep_getsizeof()`는 순환 참조를 `seen` 집합으로 막으며 dict, sequence,
`__dict__`, `__slots__`를 따라갑니다. 이는 Python 객체 크기의 근사값입니다.
Redis의 allocator, fragmentation, 공유 객체를 동일하게 모델링하지 않습니다.

## 5. 자료구조

### Hash

`store/hash_table.py`에는 두 구현이 있습니다.

- `ChainedHashTable`: 런타임 Hash 명령에서 사용
- `OpenAddressHashTable`: 충돌·tombstone·resize 특성 비교와 회귀 테스트용

둘 다 직접 구현한 MurmurHash3 32-bit 결과를 사용하고 capacity를 2의
거듭제곱으로 유지합니다. 현재 런타임은 separate chaining을 선택합니다.

### List와 Set

List는 `collections.deque`, Set은 Python `set`을 사용합니다. 이 프로젝트의
학습 초점은 명령 의미, 범위 처리, TTL·메모리·영속성 계약에 있습니다.

### Sorted Set

`store/skiplist.py`의 `ZSet`은 두 구조를 결합합니다.

- `member -> score` dict: 점수 조회
- skiplist: score와 member 순서, 범위와 rank

점수 변경은 기존 skiplist 노드를 제거하고 새 점수로 삽입합니다. 동일 점수의
순서는 member 문자열로 결정합니다. 성능 비교에서 이 경로가 기준 Redis와
가장 큰 차이를 보였습니다.

## 6. TTL

`store/expiry.py`는 절대 만료 시각을 별도 dict에 저장합니다.

- Lazy expiry: 키를 읽을 때 만료 여부 확인
- Active expiry: 백그라운드 루프가 만료 키 일부를 무작위 표본으로 제거

전체 키를 매 주기 순회하지 않습니다. 표본의 만료 비율이 낮아지면 현재
pass를 멈추고, 한 주기의 최대 pass 수도 제한합니다.

키 삭제 hook은 TTL 메타데이터를 같이 제거합니다. TTL 추가와 제거는 해당
키의 메모리 추정치만 갱신합니다.

## 7. AOF와 snapshot

`store/persistence.py`는 성공한 쓰기 결과만 기록합니다.

- AOF: canonical RESP 명령을 append
- snapshot: `MINIRDB1` 헤더 뒤에 현재 상태를 복원할 명령을 기록
- replay: 별도 복원 코드를 두지 않고 실제 디스패처를 다시 통과

상대 TTL은 파일에 저장될 때 `PEXPIREAT` 절대 시각으로 바뀝니다. 재생 중
자동 삭제나 추가 AOF 기록이 발생하지 않도록 persistence를 일시 중지합니다.

이 snapshot은 Redis RDB 바이너리 포맷과 호환되지 않습니다.

## 8. 검증 계약

테스트를 세 층으로 나눴습니다.

1. 단위 테스트: 파서, 인코더, 명령, 자료구조
2. 통합 테스트: 실제 TCP 서버와 분할/연속 프레임
3. 차등 검증: 고정한 Redis 7.4.9와 53개 대표 명령 결과 비교

차등 검증의 명령 목록은
`tests/test_benchmark_contracts.py`에서 실제 `COMMAND_TABLE`과 비교합니다.
명령을 추가하고 차등 사례를 추가하지 않으면 테스트가 실패합니다.

성능 검증기는 예외를 “미구현이므로 skip”하지 않습니다. 워밍업, 측정,
응답 후조건 중 하나라도 실패하면 결과 파일을 성공으로 남기지 않고
프로세스를 실패시킵니다.
