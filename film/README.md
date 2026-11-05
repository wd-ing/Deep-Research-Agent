# film/ — 宣传片制作目录

`final_zh_4k60.mp4` / `final_en_4k60.mp4`：成片（4K60，31.8s，中英双版）。
`archive/`：历史草稿（draft29/draft_en/verify29）。

## 文件
- `SCRIPT.md`：分镜脚本（逐秒旁白与画面）
- `compose.py`：FX 配乐合成（音乐为授权曲目 `music/dta.wav`）
- `score.py`：「一根线」配乐——连续 ambient 底 + 动作音效
- `build_cjk.py`：CJK 字体子集化，输出 cjk.js（与 look.js 同套路）
- `dump_events.py`：从合成导出 `__events()` 到 events.json（系统 python3 跑）
- `snap.py`：按时间点截帧 → /tmp/dr-fX.png
- `errcheck.py`、`events.json`、`fx_score.json`、`fonts/`、`music/`、`thread/`、`sfx.wav`、`fx.wav`：制作素材与产物

## 网页版
`homepage/build_film_web.sh` 把成片压成 1080p 网页版 + poster，输出到 `homepage/film/`（mp4 不入库）。
