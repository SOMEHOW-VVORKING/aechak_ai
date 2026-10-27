#!/usr/bin/env bash
#
# 초안 하네스의 이미지 픽스처를 만든다. **바이너리를 레포에 커밋하지 않는다.**
#
# 50 케이스 x 5 등급 JPEG 이 레포를 수십 MB 불린다. 그래서 이 스크립트만 커밋하고
# 산출물은 gitignore 한다. 픽스처가 없으면 하네스가 (나)·gated 모드를 **건너뛰고 이유를
# 적고 종료 코드 0** 이다 -- 조용히 비어 있는 것이 더 나쁘다.
#
# 쓰는 도구는 macOS 의 sips 또는 ImageMagick 이다. **둘 다 시스템 도구이고 파이썬 의존성이
# 아니다** -- 운영 이미지에 Pillow 를 들이지 않기로 한 결정과 같은 이유다. AI 서버는
# 리사이즈를 하지 않고, 축소는 셀러센터가 하며, 여기서는 측정용 입력만 만든다.
#
# 사용법
#
#   1) 케이스마다 원본 사진 한 장을 아래 경로에 둔다(긴 변 1500px 이상 권장).
#
#        harness/draft_images/_src/<case_id>.jpg
#
#      케이스 id 는 harness/draft_cases.py 의 것을 그대로 쓴다. 사진 등급은 케이스가
#      적어 둔 image_variant 를 **사람이 사진으로 표현해야 한다** --
#        GOOD  그 상품이 또렷하게 한 개
#        DARK  같은 상품을 어둡게/흐리게 (노출 부족)
#        MULTI 여러 상품이 함께 찍힌 사진
#        WRONG 상품명과 **다른 상품** 사진  <- 오사진 안전율의 분모다. 반드시 다르게
#
#   2) 이 스크립트를 돌린다.
#
#        bash harness/make_draft_images.sh
#
#   3) 하네스 자기검사가 등급별 긴 변이 선언과 맞는지 확인한다. 어긋나면 측정 전에 멈춘다 --
#      치수가 틀리면 토큰 예산이 틀리고 비용 비교가 무의미해진다.
#
# 등급을 바꾸려면 harness/run_draft.py 의 VARIANTS 와 아래 EDGES 를 함께 고쳐야 한다.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$ROOT/draft_images/_src"
EDGES=(640 768 896 1024 1500)

if [[ ! -d "$SRC" ]]; then
  echo "원본 사진 폴더가 없다: $SRC"
  echo "케이스마다 <case_id>.jpg 를 그 폴더에 두고 다시 돌려라."
  exit 1
fi

resize() {
  local input="$1" output="$2" edge="$3"
  if command -v sips >/dev/null 2>&1; then
    # sips 는 긴 변을 기준으로 맞춘다. 비율을 유지한다.
    sips --resampleHeightWidthMax "$edge" "$input" --out "$output" >/dev/null
  elif command -v magick >/dev/null 2>&1; then
    magick "$input" -resize "${edge}x${edge}>" -quality 85 "$output"
  elif command -v convert >/dev/null 2>&1; then
    convert "$input" -resize "${edge}x${edge}>" -quality 85 "$output"
  else
    echo "sips 도 ImageMagick 도 없다. 둘 중 하나를 설치해라." >&2
    exit 1
  fi
}

count=0
for input in "$SRC"/*.jpg "$SRC"/*.jpeg "$SRC"/*.png; do
  [[ -e "$input" ]] || continue
  base="$(basename "$input")"
  case_id="${base%.*}"
  out_dir="$ROOT/draft_images/$case_id"
  mkdir -p "$out_dir"
  for edge in "${EDGES[@]}"; do
    resize "$input" "$out_dir/$edge.jpg" "$edge"
  done
  count=$((count + 1))
  echo "  $case_id -> ${EDGES[*]}"
done

echo
echo "케이스 $count 건의 픽스처를 만들었다."
echo "다음: uv run python -m harness.run_draft   (자기검사가 치수를 확인한다)"
