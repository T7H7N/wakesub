# wakesub

쉬고 있는 에이전트 세션은 시각이 되거나 다른 프로세스가 파일을 썼을 때 스스로 깨어날 수 없다. wakesub은 세션 밖에서 조건을 주기적으로 확인하고 로컬 명령이나 수신 폴더에 짧은 메시지를 보낸다. Python 3.9 이상에서 표준 라이브러리만 사용한다.

## 세션이 스스로 깨움을 거는 세 가지 장면

세션은 쉬기 전에 아래 명령 중 하나를 실행한다. 세션 밖에서 실행 중인 wakesub이 조건을 확인하고 설정한 어댑터로 통지를 보낸다.

1. **다른 일꾼에게 맡긴 결과가 파일에 쓰인다.** 빈 파일이어도 되는 완료 표시는 `--file`, 내용이 들어와야 하는 결과 파일은 `--file-nonempty`를 쓴다.

   ```sh
   python -B -m wakesub add --file-nonempty report.md --target demo --say "일꾼이 report.md에 결과를 쓰기 시작했습니다."
   ```

   통지 예시 — “일꾼이 report.md에 결과를 쓰기 시작했습니다.”

2. **정한 시각이 된다.** 오늘 아직 지나지 않은 시각이나 날짜와 시각을 함께 넣는다.

   ```sh
   python -B -m wakesub add --at 16:30 --target demo --say "점검을 예약한 16시 30분이 되었습니다."
   ```

   통지 예시 — “점검을 예약한 16시 30분이 되었습니다.”

3. **앞 깨움이 발화한다.** `PREVIOUS_ID`를 앞서 실행한 `add`의 구독 ID로 바꾼다.

   ```sh
   python -B -m wakesub add --after PREVIOUS_ID --target demo --say "앞 깨움 구독이 발화했습니다."
   ```

   통지 예시 — “앞 깨움 구독이 발화했습니다.”

강한 모델은 주입된 명령문을 거부할 수 있으므로 통지는 관측한 사실로 쓴다. [Claude Code 예시](examples/claude-code/README.md) · [English README](README.md)

## 빠른 시작

이 README가 있는 폴더에서 아래 다섯 줄을 실행한다. Windows에서는 동봉한 실행기를 PowerShell에서 `./wakesub.cmd`, 명령 프롬프트에서 `.\wakesub.cmd`로 실행할 수 있다.

```sh
python -B -m wakesub add --file-nonempty ready.txt --target demo --say "The report is available at ready.txt."
python -c "from pathlib import Path; Path('ready.txt').write_text('Report body', encoding='utf-8')"
python -B -m wakesub --config examples/file.json run-once
python -B -m wakesub list
python -c "from pathlib import Path; print(max(Path('examples/inbox').glob('*.md'), key=lambda p: p.stat().st_mtime_ns).read_text(encoding='utf-8'))"
```

`add`가 구독 ID를 표시한다. `drop`으로 구독을 지우거나 다른 구독의 `--after`에 이 ID를 넣는다. 아직 열린 후속 구독이 있으면 이를 지우거나 종료 단계에 도달한 뒤 선행 구독을 지울 수 있다. 공통 옵션 `--store`, `--config`는 하위 명령 앞에 쓴다. 기본값은 작업 폴더의 `.wakesub`, `wakesub.json`이다. `run-once`와 `daemon`만 설정 파일을 읽는다.

## 조건

| 옵션 | 충족 조건 | 기본 만료 |
| --- | --- | --- |
| `--at TIME` | 지정 시각에 도달했다. 등록할 때는 미래여야 한다. | 없음 |
| `--file PATH` | 경로에 일반 파일이 있다. 0바이트도 해당한다. | 6시간 |
| `--file-nonempty PATH` | 경로에 내용이 1바이트 이상인 일반 파일이 있다. | 6시간 |
| `--after ID` | 선행 구독이 `fired`에 도달했다. | 2일 |

조건은 하나만 지정한다. `--say`는 표시 가능한 문자 1~300자로 관측한 사실과 관련 경로나 시각을 쓴다. `--target`은 표시 가능한 문자 1~200자로 어댑터가 해석한다. 감시할 상대 경로는 등록 시 작업 폴더를 기준으로 고정된다.

`--at`과 `--until`은 시스템 현지 시각의 오늘 `HH:MM` 또는 초를 생략할 수 있는 ISO 날짜와 시각을 받는다. 시계 전환을 전후해 등록할 때는 시간대 오프셋을 명시한다. `--ttl`은 `6h`, `1.5d` 같은 등록 시점부터의 양수 기간이다. `--ttl` 또는 `--until`로 기본 만료를 바꾼다. 시각 조건의 만료는 지정 시각보다 늦어야 한다. 기한까지 조건을 관측하지 못한 회차는 만료 표시가 붙은 메시지를 대기열에 올린다. `--quiet-expire`를 주면 메시지 없이 만료를 기록한다. 한번 관측해 기록한 이벤트 판정은 배달 유예와 재시도에도 유지된다.

## 배달 어댑터

설정은 JSON 객체다. `budget`은 회차당 배달 시도 상한으로 기본 5다. `target_interval`은 같은 저장소에서 같은 대상으로 배달을 시도할 때의 최소 간격으로 기본 30초다. 0으로 끌 수 있다. `max_attempts`는 최초 시도를 포함하며 기본 3이다. `interval`은 상주 실행에서 회차가 끝난 뒤 쉬는 시간으로 기본 10초다. 재시도와 만료 메시지도 예산을 쓰며 조용한 만료는 배달을 시도하지 않는다.

- **`command`**는 기본 어댑터 유형이다. `adapter.argv`에 고정 실행 파일로 시작하는 인자 배열을 지정한다. 나머지 인자에는 `{target}`, `{message}`, `{id}`, `{event}`를 쓸 수 있다. `{message}`가 있으면 해당 인자로 전달한다. 그 외에는 UTF-8 표준 입력으로 보낸다. 셸을 거치지 않고 실행한다. Windows에서는 네이티브 실행 파일을 쓴다. `adapter.timeout` 기본은 30초다. 종료 코드 0을 수신 성공으로 처리하고 출력은 버린다. 0이 아닌 종료 코드에서 재시도해도 안전한 명령을 쓰거나 수신 쪽에서 `{id}`로 중복을 제거한다.
- **`file`**은 `adapter.inbox`에 구독 ID를 이름으로 한 UTF-8 마크다운 파일 하나를 쓴다. 대상과 메시지가 들어 있다. 상대 수신 경로는 설정 파일이 있는 폴더 기준이다. 수신 프로세스는 이 폴더에서 `*.md`를 감시하고 읽을 수 있다. `adapter.signal`을 `true`로 두면 통지를 쓴 뒤 같은 폴더의 `signal` 파일을 건드린다. 이 방식에서 수신 쪽은 통지를 `<id>.delivered`로 바꿔 읽음 처리하고 재전달을 막을 수 있다. [파일 설정](examples/file.json)
- **`noop`**은 외부 동작 없이 성공을 반환한다. `{"adapter": {"type": "noop"}}`으로 상태 전이를 점검할 수 있다.

명령 설정은 `claude -p --resume <session-id>` 꼴을 보여 준다. `<session-id>` 자리에 `{target}`이 들어가고 메시지는 표준 입력으로 보낸다. 자기 CLI에 맞춰 고치는 예시다. 설치한 실행 파일의 도움말에서 플래그와 입력 방식을 확인한다. [명령 설정](examples/command.json)

새 어댑터에는 `deliver(delivery)`를 구현하고 `wakesub.engine.run_once`에 객체를 전달한다. `Delivery`는 `subscription_id`, `target`, `event`, `message`를 가진다. 수신 성공 시 반환한다. 재시도가 안전하면 `DeliveryError`를, 이미 전달됐을 수 있으면 `DeliveryUncertain`을 발생시킨다. 예상하지 못한 어댑터 오류도 전달 여부가 불명확한 것으로 처리한다. `clock` 인자로 유닉스 에포크 기준 초를 반환하는 가상 시계를 주입할 수 있다.

```python
from pathlib import Path
from wakesub.engine import run_once
from wakesub.store import Store

class RecordingAdapter:
    def __init__(self):
        self.messages = []

    def deliver(self, delivery):
        self.messages.append(delivery)

recording = RecordingAdapter()
summary = run_once(Store(Path("scratch-state")), recording)
```

## 예약 실행

- **Windows 작업 스케줄러**에서 반복 트리거를 만든다. 동작의 **프로그램/스크립트**에는 Python 실행 파일, **인수 추가**에는 `-B -m wakesub --config examples/file.json run-once`, **시작 위치**에는 이 README가 있는 폴더를 지정한다. 감시 경로를 읽을 수 있고 저장소와 수신 폴더에 쓸 수 있는 계정으로 실행한다. 이미 인스턴스가 실행 중이면 새 인스턴스를 시작하지 않는 설정을 쓴다.
- **cron**에서 홈 폴더 아래 `tools/wakesub`에 공개판을 뒀다면 아래 항목으로 매분 감시한다. 실패를 확인할 수 있는 곳에 예약 실행 출력을 남긴다.

```cron
* * * * * cd "$HOME/tools/wakesub" && python3 -B -m wakesub --config examples/file.json run-once >> wakesub.log 2>&1
```

포그라운드 상주 실행은 `python -B -m wakesub --config examples/file.json daemon`이다. `run-once`는 다른 프로세스가 저장소에 접근 중이면 오류로 종료한다. `daemon`은 잠금 충돌을 기록하고 다음 회차에 다시 시도한다. 두 명령은 회차 집계를 출력한다. 최종 배달 실패는 상태에 기록하고 표준 오류에 남긴다. 실패 구독을 처리한 회차 자체는 성공으로 종료한다. 인자 오류는 종료 코드 2, 설정·잠금·저장소 오류는 코드 1이다.

## 설계 메모

- 생산자가 내용을 쓰기 전에 0바이트 파일을 만들 수 있다. 빈 파일이 성급하게 깨우는 상황에서는 `--file-nonempty`를 쓴다.
- 등록 전에 파일이 이미 있을 수 있다. 조건에 맞는 경로는 다시 생성될 때까지 기다리지 않고 다음 배달 가능한 회차에 깨운다.
- 오늘 이미 지난 시각을 넣으면 즉시 깨어나거나 내일로 밀릴 수 있다. 등록은 미래 시각만 받는다.
- 감시 중 파일이 사라지거나 읽기 권한을 잃으면 해당 회차에서 파일 조건을 관측하지 않은 것으로 처리한다.
- 구독 하나에 JSON 파일 하나를 쓴다. 임시 파일을 쓴 뒤 원자적으로 교체한다. 폴더 잠금이 판정과 배달을 직렬화해 겹친 회차가 같은 구독을 두 번 보내지 않는다. 저장소에는 잠금 파일과 대상별 시도 시각도 있다.
- 상태는 `open`에서 `fired`, `expired`, `failed` 중 하나로 바뀐다. 만료 메시지를 성공적으로 보내면 `expired`가 된다. 만료 메시지 전송이 실패하면 의도한 이벤트를 보존한 채 `failed`가 될 수 있다. 중단 뒤 시도 중 기록이 발견되면 다시 보내지 않고 `failed`로 남긴다. 명령 시간 초과도 전달 여부가 불명확한 것으로 처리한다.
- 대상 세션이 떠 있지 않음을 감지하는 어댑터는 실패를 반환한다. 허용된 시도가 끝나거나 결과가 불확실하면 본체는 `failed`로 남긴다. 앱 주소를 열거나 CLI에서 이어 가는 방식으로 다시 띄울 때는 어댑터에 하루 기동 상한을 둔다.
- 사람이 방금 입력했다면 수신 쪽에서 깨움을 미룰 수 있다. 본체는 회차 예산과 대상별 간격을 적용한다.
- `python -B -m wakesub prune`은 `fired`와 `expired` 기록을 8일 뒤, `failed` 기록을 30일 뒤 정리한다. `--done-days N`과 `--failed-days M`에 0 이상의 정수로 일수를 지정할 수 있다. 종료 시각을 기준으로 경과일을 계산하며, 이전 판 기록은 파일의 마지막 수정 시각을 쓴다. 열린 구독과 그 구독이 참조하는 선행 기록은 보존한다.

## 한계

회차 사이에 생겼다가 사라지는 파일은 놓칠 수 있다. 내용이 있어도 파일이 완성됐다고 볼 수 없다. 완성이 중요하면 별도 완료 신호 파일을 만들거나 완성한 파일 이름을 원자적으로 바꾼다. 배달 시점은 회차, 예산, 실행 명령의 소요 시간과 시스템 시계에 영향을 받는다.

어댑터가 확인하는 것은 명령 종료나 파일 전달까지다. 실제 세션이 재개돼 행동했는지는 수신 쪽에서 확인한다. 시도 기록과 결과 기록 사이에 중단되면 메시지를 이미 받았을 수도, 받지 못했을 수도 있다. 이 경우 확인할 수 있도록 실패 상태로 둔다. 메시지를 받고도 명령이 실패 코드로 종료하면 수신 쪽에서 중복을 제거하지 않는 한 재시도가 중복 메시지를 보낼 수 있다.

로컬 파일시스템과 신뢰할 수 있는 설정, 저장소, 예약 실행 작업 폴더를 사용한다. 여러 기기 사이 잠금과 전원 손실 후 복구는 지원 범위 밖이다. 종료 기록은 `drop`이나 `prune`을 실행할 때까지 보존한다. 사용한 대상당 마지막 시도 시각 하나가 남는다. 명령 출력은 버린다. 명령 시간 초과는 분리된 자식 프로세스까지 제어하지 않는다. 서로 독립된 대기열은 별도 저장소를 쓴다.

## 포함하지 않은 기능

- 메일 서고 조건.
- 업무 장부 연동.
- 채팅 봇 알림.
- 운영 절차 실행.

## 관련 작업

- cc-peer는 실행 중인 Claude Code 세션의 수신 소켓으로 메시지를 보낸다. [cc-peer](https://pypi.org/project/cc-peer/)
- raw-async-delivery (pi-shell-acp)는 실행 중인 에이전트 세션에 비동기로 배달하고, 쉬는 Claude Code 세션은 `FileChanged`와 `watchPaths`로 깨운다. 이 공개판의 훅 예시가 참고한 선례다. [패키지](https://www.npmjs.com/package/@junghanacs/pi-shell-acp)
- TaskWake는 사용량 한도나 과부하로 멈춘 세션을 여유가 돌아오면 `claude --resume`으로 이어 간다. [TaskWake](https://github.com/Mira190/TaskWake)
- herdctl은 일정, 웹훅, 채팅으로 새 에이전트 작업을 띄운다. [herdctl](https://github.com/edspencer/herdctl)
- unsnooze와 claude-powernap은 사용량 한도가 풀리면 같은 세션을 이어 간다. [unsnooze](https://github.com/saaranshM/unsnooze) · [claude-powernap](https://pypi.org/project/claude-powernap/)

wakesub은 시각·파일·앞 구독을 조건으로 받아 만료도 통지하고 회차 예산을 적용한다. 위 도구들이 제공하는 배달 방식이 맞으면 어댑터로 연결할 수 있다.
