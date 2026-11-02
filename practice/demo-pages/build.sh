#!/bin/bash
# Inlines data.js into the two demo pages: replay.html and cortex.html.
set -e
cd "$(dirname "$0")"
for p in replay cortex; do
  python3 -c "import sys;src=open('$p.src.html').read();data=open('data.js').read();open('$p.html','w').write(src.replace('<script>/*DATA*/</script>','<script>'+data+'</script>'))"
done
ls -la replay.html cortex.html | awk '{print $5, $9}'
