#!/bin/bash
# 把 film/ 下 4K 成片压成网页版（H.264/yuv420p/faststart/1920宽/AAC128k）。
# 目标每个 ≤12MB：CRF 23 起步，超了自动加，最多 27（脚本顶部写死上限）。
# 已存在即跳过；--force 重转。
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p film
CRF=${CRF:-23}; MAX_CRF=27
[ "${1:-}" = "--force" ] && rm -f film/film_zh_1080.mp4 film/film_en_1080.mp4 \
                              film/poster_zh.jpg film/poster_en.jpg
for lang in zh en; do
  src="film/final_${lang}_4k60.mp4"   # 源在仓库根 film/，软链/拷贝均可
  [ -f "$src" ] || src="../film/final_${lang}_4k60.mp4"
  out="film/film_${lang}_1080.mp4"
  if [ -f "$out" ]; then echo "skip $out"; else
    crf=$CRF
    while :; do
      ffmpeg -y -i "$src" -map 0:v -map 0:a? \
        -vf scale=1920:-2 -c:v libx264 -preset slow -crf "$crf" \
        -pix_fmt yuv420p -movflags +faststart -c:a aac -b:a 128k \
        "$out" -v error -stats
      sz=$(stat -f %z "$out")
      if [ "$sz" -le 12582912 ] || [ "$crf" -ge "$MAX_CRF" ]; then
        echo "$out  crf=$crf  ${sz}B"; break
      fi
      crf=$((crf+1)); echo "  $((sz/1048576))MB >12MB, 提到 crf=$crf"
    done
  fi
  pos="film/poster_${lang}.jpg"
  [ -f "$pos" ] || ffmpeg -y -ss 3 -i "$src" -frames:v 1 \
    -vf scale=1280:-2 -q:v 4 "$pos" -v error
done
echo done
