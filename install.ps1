# 컴비서(Combiyso) Windows 원클릭 설치 스크립트
#   사용법(PowerShell):
#     irm https://raw.githubusercontent.com/kkang2da-sys/combiyso/main/install.ps1 | iex
#   옵션:
#     $env:COMBIYSO_NORUN = "1"   # 설치 후 자동 실행하지 않음
#     $env:COMBIYSO_VERSION = "v1.6.4"  # 특정 버전 설치

$ErrorActionPreference = "Stop"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

$Repo  = "kkang2da-sys/combiyso"
$Api   = "https://api.github.com/repos/$Repo/releases/latest"
$Heads = @{ "User-Agent" = "combiyso-install"; "Accept" = "application/vnd.github+json" }

function Write-Step($msg) { Write-Host "  $msg" -ForegroundColor DarkGray }
function Write-Ok($msg)   { Write-Host "OK  $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "!   $msg" -ForegroundColor Yellow }

Write-Host ""
Write-Host "  컴비서 설치" -ForegroundColor Cyan
Write-Host "  ------------------------------------------" -ForegroundColor DarkGray

# 1) 릴리스 정보
Write-Step "최신 버전 확인 중..."
try {
    $rel = Invoke-RestMethod -Uri $Api -Headers $Heads -TimeoutSec 30
} catch {
    Write-Host "릴리스를 가져오지 못했습니다: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "인터넷 연결을 확인하거나 https://github.com/$Repo/releases 에서 직접 받으세요."
    return
}
Write-Ok "버전 $($rel.tag_name)"

$setup = $rel.assets | Where-Object { $_.name -eq "combiyso-windows-setup.exe" } | Select-Object -First 1
$zip   = $rel.assets | Where-Object { $_.name -eq "combiyso-windows-x64.zip" } | Select-Object -First 1
if (-not $setup -and -not $zip) {
    Write-Host "릴리스에 Windows 설치 파일이 없습니다." -ForegroundColor Red
    return
}

$tmp = Join-Path $env:TEMP ("combiyso-setup-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp -Force | Out-Null

try {
    if ($setup) {
        # 2-A) 설치 프로그램(권장) — 시작 메뉴/제거 정보까지 등록
        $file = Join-Path $tmp "combiyso-setup.exe"
        Write-Step "다운로드 중... ($([math]::Round($setup.size/1MB,1)) MB)"
        Invoke-WebRequest -Uri $setup.browser_download_url -OutFile $file -UseBasicParsing -TimeoutSec 600
        Unblock-File $file -ErrorAction SilentlyContinue
        Write-Step "설치 중... (관리자 권한 불필요)"
        # 실행 중인 컴비서가 있으면 먼저 종료 (파일 잠금으로 설치가 취소되는 것을 방지)
        Get-Process -Name "컴비서", "combiyso" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 900
        $p = Start-Process -FilePath $file -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/FORCECLOSEAPPLICATIONS" -Wait -PassThru
        if ($p.ExitCode -ne 0) { throw "설치 프로그램이 코드 $($p.ExitCode) 로 종료되었습니다." }
        $dest = Join-Path $env:LOCALAPPDATA "Programs\Combiyso"
        $exe  = Join-Path $dest "컴비서.exe"
        if (-not (Test-Path $exe)) { throw "설치 후 실행 파일을 찾지 못했습니다: $exe" }
        Write-Ok "설치 완료: $dest"
    } else {
        # 2-B) 포터블 zip 으로 직접 설치
        $file = Join-Path $tmp "combiyso.zip"
        Write-Step "다운로드 중... ($([math]::Round($zip.size/1MB,1)) MB)"
        Invoke-WebRequest -Uri $zip.browser_download_url -OutFile $file -UseBasicParsing -TimeoutSec 600
        Unblock-File $file -ErrorAction SilentlyContinue
        Expand-Archive -Path $file -DestinationPath (Join-Path $tmp "app") -Force
        $dest = Join-Path $env:LOCALAPPDATA "Programs\Combiyso"
        if (Test-Path $dest) {
            Get-Process -Name "컴비서", "combiyso" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 800
            Remove-Item $dest -Recurse -Force -ErrorAction SilentlyContinue
        }
        New-Item -ItemType Directory -Path $dest -Force | Out-Null
        Copy-Item (Join-Path $tmp "app\*") $dest -Recurse -Force
        Get-ChildItem $dest -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
        $exe = Join-Path $dest "컴비서.exe"

        # 바로가기(바탕화면 + 시작 메뉴)
        $ws = New-Object -ComObject WScript.Shell
        $links = @(
            (Join-Path ([Environment]::GetFolderPath('Desktop')) "컴비서.lnk"),
            (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\컴비서.lnk")
        )
        foreach ($l in $links) {
            try {
                $sc = $ws.CreateShortcut($l)
                $sc.TargetPath = $exe
                $sc.WorkingDirectory = $dest
                $sc.IconLocation = $exe
                $sc.Save()
            } catch { Write-Warn2 "바로가기 만들기 실패: $l" }
        }
        Write-Ok "설치 완료: $dest"
    }

    Write-Host ""
    Write-Host "  실행: $exe" -ForegroundColor Cyan
    Write-Host "  종료: 검은 콘솔 창을 닫거나 작업 표시줄 트레이에서 종료" -ForegroundColor DarkGray
    Write-Host "  제거: 설정 > 앱 > 컴비서 (또는 $dest\unins000.exe)" -ForegroundColor DarkGray
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue) -and -not (Test-Path "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe")) {
        Write-Host ""
        Write-Warn2 "Ollama 가 설치되어 있지 않습니다. 로컬 모델을 쓰려면 https://ollama.com/download"
    }
    Write-Host ""
    if ($env:COMBIYSO_NORUN -ne "1") {
        Write-Step "컴비서를 시작합니다..."
        Start-Process -FilePath $exe -WorkingDirectory $dest
    }
} catch {
    Write-Host ""
    Write-Host "설치 실패: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "수동 설치: https://github.com/$Repo/releases" -ForegroundColor Yellow
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
