Script for automatically generating score videos. (Windows only, maybe it works for linux and mac too.)

This script requires [poppler](https://github.com/oschwartz10612/poppler-windows/releases) and [ffmpeg](https://www.ffmpeg.org/).

Required python libraries: `pip install pdf2image Pillow`

Usage: `python auto_sv.py [pdf dir] [wav dir] [output dir]`. A browser window will open where you can time your page turns.

Examples of generated videos are under `convergence/` and `infernal/`, the only thing you need is `auto_sv.py`.

This was entirely vibe coded, so maybe don't trust it too much.
