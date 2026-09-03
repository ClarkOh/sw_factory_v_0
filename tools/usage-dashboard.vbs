' Claude Code 사용량 대시보드 자동 시작
'
' 왜 vbs 인가: .bat 를 시작프로그램에 두면 로그인마다 검은 콘솔 창이 뜬다.
' pythonw + WScript 0 모드는 창 없이 조용히 뜬다.
' 서버는 이미 떠 있으면 포트 충돌로 그냥 죽으므로 중복 실행 걱정은 없다.
'
' 지우려면 이 파일을 삭제하면 된다:
'   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\usage-dashboard.vbs
CreateObject("WScript.Shell").Run _
  """C:\ProgramData\anaconda3\pythonw.exe"" ""C:\work\sw_factory\tools\usage_server.py"" --no-open", _
  0, False
