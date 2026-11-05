#!/bin/sh
# Assemble the GitHub Pages site in _site/ from the repo's own files, so the playground always runs the real code:
# the orb (desktop/renderer), the teaching board (desktop/board) and the privacy scrubber (lantern/scrubber.py).
set -eu
cd "$(dirname "$0")/.."
rm -rf _site
mkdir -p _site/app/lib _site/py _site/media _site/renderer/lib
cp site/index.html _site/
cp desktop/renderer/orb.css _site/app/
cp desktop/renderer/lib/sprite.js _site/app/lib/
cp -R desktop/board _site/board
cp desktop/renderer/lib/talk.js _site/renderer/lib/          # the board loads it for "hold to ask"
cp lantern/scrubber.py _site/py/
cp docs/media/lantern-30s.mp4 docs/media/social-preview.png _site/media/
touch _site/.nojekyll
echo "site assembled in _site/"
