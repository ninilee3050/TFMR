# TFMR (스캐너 + 백테스트)

이 저장소에는 아래가 포함되어 있습니다.
- 소스 코드: `tfmr_min_scanner_gui.py`
- 실행/빌드 보조 스크립트: `run_dev.bat`, `build_exe.bat`
- 바로 실행 가능한 Windows 번들: `dist/TFMR/TFMR.exe`

사용 방식은 2가지입니다.
1. 실행만 하기 (Python 설치 없이 사용)
2. 개발/수정하기 (코드 수정 + 빌드 + Git 업로드)

---

## 1) 실행만 할 때 (가장 쉬움)

코드 수정 없이 프로그램만 실행하려면 아래 순서대로 하세요.

1. 저장소 접속: `https://github.com/ninilee3050/TFMR`
2. `Code` -> `Download ZIP` 클릭
3. 압축 해제
4. `dist\TFMR\TFMR.exe` 더블클릭 실행

참고:
- 처음 실행 시 Windows SmartScreen 경고가 뜰 수 있습니다.
- 이 방식은 Python, VS Code가 없어도 실행됩니다.

---

## 2) 개발/수정할 때 (추천 방식: clone)

코드를 수정해서 다시 GitHub에 올릴 계획이면 ZIP 말고 `git clone`을 쓰는 것이 맞습니다.

### 준비물
- Git
- Python 3.x (Windows 권장: 3.14)
- VS Code (선택)

### 프로젝트 받기 (clone)
```powershell
cd C:\Users\user\Desktop
git clone https://github.com/ninilee3050/TFMR.git
cd TFMR
```

### 소스 코드로 실행
```powershell
cd C:\Users\user\Desktop\TFMR
run_dev.bat
```

수동 실행 명령:
```powershell
python tfmr_min_scanner_gui.py
```

---

## 3) EXE 다시 빌드하기

코드를 수정한 뒤 실행파일을 새로 만들 때:

```powershell
cd C:\Users\user\Desktop\TFMR
build_exe.bat
```

`build_exe.bat` 동작:
1. PyInstaller 설치 확인
2. 없으면 자동 설치
3. EXE 번들 빌드 (`--onedir`)
4. 결과 생성:
   - `dist\TFMR\TFMR.exe`
   - `dist\TFMR\_internal\...`

중요:
- 빌드하면 같은 경로에 새 결과가 생성됩니다.
- 기존 `dist\TFMR\TFMR.exe`는 새 빌드로 덮어써집니다.

---

## 4) GitHub에 수정사항 올리기

```powershell
cd C:\Users\user\Desktop\TFMR
git add -A
git commit -m "설명"
git push origin main
```

처음 한 번만 사용자 정보 설정이 필요할 수 있습니다.
```powershell
git config --global user.name "YOUR_GITHUB_ID"
git config --global user.email "YOUR_EMAIL"
```

---

## 5) ZIP vs Clone 차이 (중요)

- ZIP 다운로드
  - 장점: 바로 실행하기 쉬움
  - 단점: `.git` 정보 없음 -> `pull/commit/push` 불가

- Git Clone
  - 장점: 수정 후 `commit/push`, 최신 변경 `pull` 가능
  - 단점: 처음에 명령어 1번 실행 필요

정리:
- 실행만: ZIP
- 수정/업로드: Clone

---

## 6) 추천 작업 순서

1. VS Code로 `TFMR` 폴더 열기
2. `tfmr_min_scanner_gui.py` 수정
3. `run_dev.bat`로 테스트
4. `build_exe.bat`로 EXE 재생성
5. `dist\TFMR\TFMR.exe` 실행 확인
6. `git add -A` -> `git commit` -> `git push`

---

## 7) 주요 파일 설명

- `tfmr_min_scanner_gui.py`: 메인 GUI 프로그램
- `run_dev.bat`: Python 소스 실행용
- `build_exe.bat`: EXE 번들 빌드용
- `dist/TFMR/`: 실행 배포 폴더 (`TFMR.exe` 포함)
- `.cache/`: 실행 중 생성되는 캐시/설정
- `.gitignore`: Git 제외 규칙

현재 Git 제외 규칙:
- `/__pycache__`
- `/build`
- `/TFMR.spec`
- `/dist/TFMR/.cache`

참고:
- 루트 `.cache/`는 프로젝트 설정 재현을 위해 Git에 포함됩니다.

---

## 8) 자주 발생하는 문제

### A) `origin does not appear to be a git repository`
원격 저장소 연결이 안 된 상태입니다.

```powershell
git remote add origin https://github.com/ninilee3050/TFMR.git
git push -u origin main
```

### B) `pyinstaller is not recognized`
`build_exe.bat`를 사용하면 해결됩니다. (`python -m PyInstaller` 방식)

### C) ZIP으로 받았는데 push가 안 됨
정상입니다. ZIP에는 Git 메타데이터가 없습니다.
수정/업로드 목적이면 `git clone`으로 다시 받으세요.

---

## 9) 백업 권장

안전하게 사용하려면:
1. GitHub를 메인 백업으로 사용
2. 필요하면 Google Drive에도 폴더 백업

권장 습관:
- 코드 수정 후: `commit + push`
- 실행만 필요할 때: 최신 `dist/TFMR` 백업 유지
