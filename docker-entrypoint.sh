#!/bin/sh
set -e

mkdir -p /data

for f in /app/*.json; do
  [ -e "$f" ] || continue
  base="$(basename "$f")"

  if [ ! -e "/data/$base" ]; then
    cp "$f" "/data/$base"
  fi

  rm -f "$f"
  ln -s "/data/$base" "$f"
done

exec python /app/webui.py
