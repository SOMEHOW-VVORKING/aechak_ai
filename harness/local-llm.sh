#!/usr/bin/env bash
# 로컬 sLLM(skt/A.X-4.0-Light)을 llama-server 로 띄운다. 선별 계층의 OpenAI 호환 경로가 쓴다.
#
#   harness/local-llm.sh            가중치를 받고(없으면) 서버를 띄운다
#   harness/local-llm.sh download   받기만 한다
#
# 가중치는 레포 밖(~/.cache/aechak-ai/models)에 둔다. 4.1GiB 라서 레포에 들어가면 안 된다.
set -euo pipefail

MODEL_DIR="${AECHAK_AI_LOCAL_MODEL_DIR:-$HOME/.cache/aechak-ai/models}"
# Q4_K_M 하나만 받는다. 64GB 맥에서 더 큰 양자화도 올라가지만 선별은 판정이 둘뿐인 분류라
# 품질 여유가 크고, 받는 시간과 디스크가 곧 재현 비용이다.
GGUF_REPO="mykor/A.X-4.0-Light-gguf"
GGUF_NAME="A.X-4.0-Light-Q4_K_M.gguf"
GGUF_PATH="$MODEL_DIR/$GGUF_NAME"

HOST="${AECHAK_AI_LOCAL_LLM_HOST:-127.0.0.1}"
PORT="${AECHAK_AI_LOCAL_LLM_PORT:-8080}"

download() {
  if [[ -f "$GGUF_PATH" ]]; then
    echo "이미 있다: $GGUF_PATH ($(du -h "$GGUF_PATH" | cut -f1))"
    return
  fi
  mkdir -p "$MODEL_DIR"
  echo "받는다: $GGUF_REPO/$GGUF_NAME -> $GGUF_PATH"
  # 이어받기(-C -)를 켜고 임시 이름으로 받은 뒤 옮긴다. 중간에 끊긴 파일이 완성본 이름으로
  # 남으면 다음 실행이 그것을 "이미 있다"로 보고 llama-server 가 로드에서 깨진다.
  curl -fL --retry 3 -C - \
    -o "$GGUF_PATH.part" \
    "https://huggingface.co/$GGUF_REPO/resolve/main/$GGUF_NAME"
  mv "$GGUF_PATH.part" "$GGUF_PATH"
  echo "받았다: $GGUF_PATH ($(du -h "$GGUF_PATH" | cut -f1))"
}

serve() {
  echo "띄운다: http://$HOST:$PORT"
  # -ngl 99 로 전 레이어를 Metal 에 올린다. -c 4096 은 선별 프롬프트 + 상품 본문 기준이고,
  # 늘리면 KV 캐시가 그만큼 늘어난다.
  exec llama-server \
    -m "$GGUF_PATH" \
    --host "$HOST" \
    --port "$PORT" \
    -ngl 99 \
    -c 4096 \
    --jinja
}

case "${1:-serve}" in
  download) download ;;
  serve) download && serve ;;
  *)
    echo "쓰임: $0 [download|serve]" >&2
    exit 2
    ;;
esac
