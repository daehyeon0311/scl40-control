# Claude Code에 붙여넣을 프롬프트

아래 폴더의 Shimadzu SCL-40/LC-40i Python 제어 GUI 개발을 이어서 진행해줘.

```text
C:\Users\IBS\Documents\Codex\2026-08-14\wd\outputs
```

작업 전에 `README.md`와 `CLAUDE_CODE_HANDOFF.md`를 전부 읽고, 현재 변경사항과
git 상태를 확인해. 실제 장비 IP는 `192.168.200.99`이고 PC Ethernet은
`192.168.200.101/24`야.

중요 규칙:

- LC-20/SCL-10AVP 시리얼 명령을 LC-40i에 사용하지 마.
- 실제 장비에 자동으로 START/STOP/SET FLOW 테스트를 보내지 마.
- 새로운 쓰기 명령은 정확한 HTTP URL과 XML을 먼저 보여주고 내 승인을 받아.
- HTTP 200만으로 물리적 동작 성공이라고 판단하지 마. Monitor XML 재조회와
  장비 전면 표시/물리 상태를 함께 확인해야 해.
- 비밀번호, SessionID, 실제 `scl40_access_pin.txt`, 로그 파일을 커밋하거나
  출력하지 마.
- 기존 GUI 디자인과 다중접속 PIN 보호, 명령 직렬화 기능을 유지해.
- dirty worktree가 있으면 기존 변경을 보존해.

우선 할 일:

1. 현재 코드와 확인된 XML 구조를 검토해.
2. SET FLOW 요청/응답/재조회 로깅을 민감정보 없이 개선해.
3. SCL Monitor의 Pump A 상태와 LC-40i 실제 REMOTE/전면 표시가 불일치하는
   원인을 읽기 전용으로 진단해.
4. 자동 테스트는 fake client만 사용해 작성해.
5. 변경 후 로컬 GUI와 읽기 전용 API를 검증하고 결과를 보고해.

