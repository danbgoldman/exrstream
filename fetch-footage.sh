#!/bin/bash
# Fetch a Tears of Steel shot. (CC) Blender Foundation | mango.blender.org
B=http://media.xiph.org/tearsofsteel/tearsofsteel-footage-exr
shot=${1:-01_1a}; kind=${2:-linear_hd}; n=${3:-240}; start=${4:-0}; out=${5:-footage/tos_hd}
mkdir -p "$out"
seq $start $((start+n-1)) | awk -v b="$B/$shot/$kind" -v s="$shot" '{printf "%s/%s_%05d.exr\n", b, s, $1}' \
  | xargs -P 12 -I{} curl -sL -m 300 -C - --output-dir "$out" -O {}
echo "done: $(ls "$out" | wc -l) files, $(du -sh "$out" | cut -f1)"
