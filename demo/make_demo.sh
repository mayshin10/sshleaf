#!/usr/bin/env bash
# Re-record docs/demo.{gif,mp4} and docs/screenshot.png against a throwaway SSH
# server ("remote-server", user alice) running in Docker.
# Needs: docker, uv, tectonic, Google Chrome. Nothing outside a temp dir is touched
# except the docs/ outputs.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
PORT=8766
SSH_PORT=2222
NAME=sshleaf-demo-remote
if [ "$(uname)" = Darwin ]; then TECTONIC_CACHE=$HOME/Library/Caches/Tectonic; else TECTONIC_CACHE=$HOME/.cache/Tectonic; fi

echo "workdir: $WORK"
(cd "$ROOT" && uv sync -q)

# 1. fake remote server
ssh-keygen -q -t ed25519 -N '' -C sshleaf-demo -f "$WORK/demo_key"
cp "$ROOT/demo/Dockerfile" "$WORK/"
cp -R "$ROOT/examples/demo-paper" "$WORK/demo-paper"
docker build -q -t "$NAME" "$WORK" >/dev/null
docker run -d --rm --name "$NAME" --hostname remote-server -p "127.0.0.1:$SSH_PORT:22" "$NAME" >/dev/null

cat > "$WORK/ssh_config" <<EOF
Host remote-server
  HostName 127.0.0.1
  Port $SSH_PORT
  User alice
  IdentityFile $WORK/demo_key
  IdentitiesOnly yes
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  LogLevel ERROR
Host gpu-cluster
  HostName gpu-cluster.example.org
EOF

# 2. sshleaf with a clean state and only the demo hosts
mkdir -p "$WORK/home"
HOME="$WORK/home" TECTONIC_CACHE_DIR="$TECTONIC_CACHE" \
  "$ROOT/.venv/bin/sshleaf" --port "$PORT" --no-browser --ssh-config "$WORK/ssh_config" &
SERVER=$!
trap 'kill $SERVER 2>/dev/null; docker stop $NAME >/dev/null 2>&1 || true' EXIT
sleep 3

# 3. record with headless Chrome (Playwright keeps its ffmpeg in the workdir)
export PLAYWRIGHT_BROWSERS_PATH="$WORK/pw"
uv run -q --with playwright playwright install ffmpeg >/dev/null
uv run -q --with playwright python "$ROOT/demo/record.py" remote-server "$WORK/video" "$PORT"
VIDEO=$(ls "$WORK"/video/*.webm)

# 4. encode
FFMPEG=$(uv run -q --with imageio-ffmpeg python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())")
"$FFMPEG" -loglevel error -y -i "$VIDEO" -c:v libx264 -pix_fmt yuv420p -crf 23 -preset slow -movflags +faststart "$ROOT/docs/demo.mp4"
"$FFMPEG" -loglevel error -y -i "$VIDEO" -vf "fps=12,scale=1000:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" "$ROOT/docs/demo.gif"
"$FFMPEG" -loglevel error -y -ss 24 -i "$VIDEO" -frames:v 1 "$ROOT/docs/screenshot.png"
echo "wrote docs/demo.mp4, docs/demo.gif, docs/screenshot.png"
