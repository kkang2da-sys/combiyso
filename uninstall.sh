#!/usr/bin/env bash
# 컴비서(Combiyso) 제거 스크립트 (macOS · Linux)
#
#   앱만 제거 (설정·대화 기록은 남김)
#     curl -fsSL https://raw.githubusercontent.com/kkang2da-sys/combiyso/main/uninstall.sh | bash
#
#   앱 + 설정·대화 기록까지 모두 삭제
#     curl -fsSL https://raw.githubusercontent.com/kkang2da-sys/combiyso/main/uninstall.sh | bash -s -- --purge
set -euo pipefail

PURGE=0
for a in "$@"; do
  case "$a" in
    --purge) PURGE=1 ;;
    -y|--yes) : ;;
  esac
done

OS="$(uname -s)"
bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '\033[32mOK\033[0m  %s\n' "$*"; }
warn() { printf '\033[33m! \033[0m  %s\n' "$*"; }

case "$OS" in
  Darwin|Linux) : ;;
  *) printf '이 스크립트는 macOS/Linux 용입니다. Windows 는 설정 > 앱 > 컴비서 에서 제거하세요.\n' ; exit 1 ;;
esac

bold ""
bold "  컴비서 제거  ($OS)"
printf '  ------------------------------------------\n'

# 1) 실행 중인 앱 종료
if command -v pkill >/dev/null 2>&1; then
  pkill -f "Contents/MacOS/combiyso" 2>/dev/null || true
  pkill -f "combiyso/app/combiyso" 2>/dev/null || true
  pkill -f "share/combiyso/app/combiyso" 2>/dev/null || true
  sleep 1
fi

remove() {
  if [ -e "$1" ] || [ -L "$1" ]; then
    rm -rf "$1" && ok "삭제: $1"
  fi
}

if [ "$OS" = "Darwin" ]; then
  remove "$HOME/Applications/컴비서.app"
  remove "$HOME/Desktop/컴비서.app"
  remove "$HOME/Applications/컴비서.command"     # 1.6.4 이하 구버전 실행 파일
  remove "$HOME/.local/bin/combiyso"
  remove "$HOME/Library/Application Support/Combiyso"   # 1.6.4 이하 설치 폴더
  if [ "$PURGE" = "1" ]; then
    remove "$HOME/Library/Application Support/compbiyso"  # 설정·세션·백업·로그
  fi
else
  remove "$HOME/.local/share/combiyso"
  remove "$HOME/.local/share/applications/combiyso.desktop"
  remove "$HOME/.local/bin/combiyso"
  if [ "$PURGE" = "1" ]; then
    remove "$HOME/.local/share/compbiyso"
  fi
fi

printf '\n'
if [ "$PURGE" = "1" ]; then
  ok "컴비서와 모든 데이터를 삭제했습니다."
else
  ok "컴비서 앱을 삭제했습니다."
  warn "설정·대화 기록은 남아 있습니다 (재설치하면 그대로 이어집니다)."
  if [ "$OS" = "Darwin" ]; then
    printf '   모두 지우려면:  --purge 옵션으로 다시 실행\n'
  else
    printf '   모두 지우려면:  --purge 옵션으로 다시 실행\n'
  fi
fi
printf '  남은 프로세스 확인:  pgrep -fl combiyso || echo "없음"\n\n'
