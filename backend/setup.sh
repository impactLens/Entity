#!/usr/bin/env bash
# One-time install for Debian/Ubuntu (including proot-distro): system packages,
# a virtualenv, Python dependencies, and the language models.
#   ./setup.sh            then:  cp .env.example .env && ./server.sh start
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
VENV="${VENV:-$HOME/venv}"
MODEL="${SPACY_MODEL:-en_core_web_sm}"

if command -v apt-get >/dev/null && [ "$(id -u)" -eq 0 ]; then
  apt-get update
  apt-get install -y python3 python3-venv python3-pip curl procps psmisc build-essential
else
  echo "Skipping apt (not root or not Debian). Need: python3, python3-venv, curl."
fi

python3 -m venv "$VENV"
# shellcheck disable=SC1091
. "$VENV/bin/activate"
pip install --upgrade pip
pip install -r requirements.txt
python -m spacy download "$MODEL"
python -m nltk.downloader punkt_tab averaged_perceptron_tagger_eng \
  maxent_ne_chunker_tab words stopwords

echo
echo "Done. Next:"
echo "  cp .env.example .env     # then edit NER_API_KEY"
echo "  ./server.sh start"
