# File: README.md
# PySandbox - Single-file Flask app + Ubuntu web terminal (ttyd)
#
# Files included:
# - app.py                : The Flask + Socket.IO single-file app (file manager, pip installer, terminal, paste-run)
# - Dockerfile            : Dockerfile for the Flask app
# - Dockerfile.ttyd       : Dockerfile for Ubuntu + ttyd web terminal
# - entrypoint-ttyd.sh    : Entrypoint script for ttyd container (reads TTYD_BASIC_AUTH)
# - docker-compose.yml    : Compose file to run both services locally
# - render.yaml           : Example Render manifest for ttyd
# - requirements.txt      : Python dependencies for Flask app
# - .env.example          : Example env variables (do NOT store secrets in repo)
#
# Quick local run (without containers):
#   python -m venv venv
#   source venv/bin/activate
#   pip install -r requirements.txt
#   python app.py
#   # open http://127.0.0.1:5000
#
# Quick local run with docker-compose:
#   cp .env.example .env
#   # Edit .env and set TTYD_BASIC_AUTH to a secure 'user:password'
#   docker-compose up --build
#   # Flask app: http://127.0.0.1:5000
#   # Ubuntu web terminal: http://127.0.0.1:7681 (Basic auth)
#
# Security notes:
# - DO NOT expose these services publicly without proper authentication and network restrictions.
# - Never hard-code passwords in the Dockerfiles or repo. Use env vars / secret managers.
# - Running untrusted code in these containers is dangerous. Use isolated environments and monitoring.
