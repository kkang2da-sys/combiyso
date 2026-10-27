#!/usr/bin/env bash
# 컴비서(Combiyso) macOS · Linux 설치 스크립트
#   사용법:
#     curl -fsSL https://raw.githubusercontent.com/kkang2da-sys/combiyso/main/install.sh | bash
#     curl -fsSL .../install.sh | bash -s -- --run      # 설치 후 바로 실행
#   환경변수:
#     COMBIYSO_VERSION=v1.6.4   특정 버전
set -euo pipefail

REPO="kkang2da-sys/combiyso"
API="https://api.github.com/repos/${REPO}/releases"
RUN_AFTER=0
[ "${1:-}" = "--run" ] && RUN_AFTER=1

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '\033[32mOK\033[0m  %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m   %s\n' "$*"; }
die()  { printf '\033[31m실패\033[0m  %s\n' "$*" >&2; exit 1; }

OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
  Darwin)
    case "$ARCH" in
      arm64)          TAG="macos-arm64" ;;
      x86_64)         TAG="macos-x64" ;;
      *) die "지원하지 않는 macOS 아키텍처: $ARCH" ;;
    esac ;;
  Linux)
    case "$ARCH" in
      x86_64|amd64)   TAG="linux-x64" ;;
      *) die "지원하지 않는 Linux 아키텍처: $ARCH (x86_64 만 지원)" ;;
    esac ;;
  *) die "지원하지 않는 운영체제: $OS (macOS/Linux 용입니다. Windows 는 install.ps1)" ;;
esac

bold ""
bold "  컴비서 설치  ($OS / $ARCH)"
printf '  ------------------------------------------\n'

command -v curl >/dev/null 2>&1 || die "curl 이 필요합니다."
command -v tar  >/dev/null 2>&1 || die "tar 가 필요합니다."

# 1) 릴리스 확인
if [ -n "${COMBIYSO_VERSION:-}" ]; then
  REL_URL="${API}/tags/${COMBIYSO_VERSION}"
else
  REL_URL="${API}/latest"
fi
printf '  최신 버전 확인 중...\n'
JSON="$(curl -fsSL -H 'User-Agent: combiyso-install' -H 'Accept: application/vnd.github+json' "$REL_URL" 2>/dev/null)" \
  || die "릴리스 정보를 가져오지 못했습니다. 인터넷 연결을 확인하세요. (${API})"

VER="$(printf '%s' "$JSON" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)"
URL="$(printf '%s' "$JSON" | tr ',' '\n' | grep -o "https://[^\"]*combiyso-${TAG}\.tar\.gz" | head -n1)"
[ -n "$URL" ] || die "릴리스 ${VER:-?} 에 ${TAG} 패키지가 없습니다."
ok "버전 ${VER:-?}"

# 2) 내려받기
TMP="$(mktemp -d 2>/dev/null || mktemp -d -t combiyso)"
trap 'rm -rf "$TMP"' EXIT
printf '  다운로드 중...\n'
curl -fsSL --retry 3 "$URL" -o "$TMP/pkg.tar.gz" || die "다운로드 실패: $URL"
tar -xzf "$TMP/pkg.tar.gz" -C "$TMP" || die "압축 해제 실패"
[ -d "$TMP/combiyso" ] || die "패키지 구조가 예상과 다릅니다."
chmod +x "$TMP/combiyso/combiyso" 2>/dev/null || true

mkdir -p "$HOME/.local/bin"

if [ "$OS" = "Darwin" ]; then
  # macOS: ~/Library/Application Support/Combiyso
  PREFIX="$HOME/Library/Application Support/Combiyso"
  mkdir -p "$PREFIX"
  rm -rf "$PREFIX/app"
  mv "$TMP/combiyso" "$PREFIX/app"
  xattr -dr com.apple.quarantine "$PREFIX/app" 2>/dev/null || true
  ln -sf "$PREFIX/app/combiyso" "$HOME/.local/bin/combiyso"

  # Finder 에서 더블클릭할 수 있는 실행 파일(.command → Terminal)
  mkdir -p "$HOME/Applications"
  LAUNCHER="$HOME/Applications/컴비서.command"
  {
    echo '#!/bin/bash'
    echo "exec \"$PREFIX/app/combiyso\" \"\$@\""
  } > "$LAUNCHER"
  chmod +x "$LAUNCHER"
  xattr -dr com.apple.quarantine "$LAUNCHER" 2>/dev/null || true

  ok "설치 완료: $PREFIX/app"
  printf '  실행: ~/Applications/컴비서.command (더블클릭) 또는 combiyso\n'
else
  # Linux: ~/.local/share/combiyso
  PREFIX="$HOME/.local/share/combiyso"
  mkdir -p "$PREFIX"
  rm -rf "$PREFIX/app"
  mv "$TMP/combiyso" "$PREFIX/app"
  ln -sf "$PREFIX/app/combiyso" "$HOME/.local/bin/combiyso"

  mkdir -p "$HOME/.local/share/applications"
  DESKTOP="$HOME/.local/share/applications/combiyso.desktop"
  cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=컴비서
Name[en]=Combiyso
Comment=내 PC·네트워크의 LLM 으로 파일 작업을 하는 AI 컴비서
Exec="$PREFIX/app/combiyso"
Terminal=true
Categories=Development;Utility;
StartupNotify=false
EOF
  chmod +x "$DESKTOP"

  ok "설치 완료: $PREFIX/app"
  printf '  실행: combiyso  (또는 앱 목록에서 "컴비서")\n'
fi

case ":$PATH:" in
  *":$HOME/.local/bin:"*) : ;;
  *) warn "PATH 에 ~/.local/bin 이 없습니다. 셸 설정에 추가하세요:"
     printf '     echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.profile\n' ;;
esac

printf '\n'
if [ "$RUN_AFTER" = "1" ]; then
  printf '  컴비서를 시작합니다...\n'
  if [ "$OS" = "Darwin" ]; then
    open "$HOME/Applications/컴비서.command" 2>/dev/null || "$PREFIX/app/combiyso" &
  else
    nohup "$PREFIX/app/combiyso" >/dev/null 2>&1 &
  fi
else
  if [ "$OS" = "Darwin" ]; then
    printf '  설정/대화 기록: %s\n' "${HOME}/Library/Application Support/compbiyso"
  else
    printf '  설정/대화 기록: %s\n' "${HOME}/.local/share/compbiyso"
  fi
  printf '  로컬 모델을 쓰려면 Ollama 설치: https://ollama.com/download\n'
  printf '  바로 실행하려면:  combiyso\n'
fi
