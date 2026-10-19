# 서드파티 고지: notebooklm-py

따까리의 NotebookLM 연동(ADR 0045)은 PyPI 패키지 `notebooklm-py`를 **정확한 버전 0.8.5로 고정**해 의존한다.
소스를 복사하지 않고 공개 API(`notebooklm.NotebookLMClient`, 공개 타입·예외)만 쓴다.
설치·사용하지 않는 선택 기능: master-token 자동 인증(headless), TLS 위장 전송(impersonate), 브라우저 쿠키 자동 추출 등 보호장치 우회 성격의 extra.

- 프로젝트: https://github.com/teng-lin/notebooklm-py
- 라이선스: MIT (아래 전문 보존)

---

MIT License

Copyright (c) 2026 Teng Lin

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
