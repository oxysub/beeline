# From the beeline directory:
#   make          # list targets
#   make dev
#   make push
#   make render          # RENDER_DEPLOY_HOOK in .env (gitignored) or environment
#   make release         # push then trigger Render
#
# From the repo root:
#   make -C beeline dev
#
# Python 3 (override if needed, e.g. PYTHON=python3.12)
.DEFAULT_GOAL := help

PYTHON ?= python3

PORT ?= 8000
REMOTE ?= origin
BRANCH ?= main

.PHONY: help dev install push render release

help:
	@echo "Beeline — targets:"
	@echo "  make dev      local server (http://127.0.0.1:$(PORT)/)"
	@echo "  make install  pip install -r requirements.txt"
	@echo "  make push     git push $(REMOTE) $(BRANCH)"
	@echo "  make render   POST to RENDER_DEPLOY_HOOK (e.g. in .env)"
	@echo "  make release  push, then render"

install:
	$(PYTHON) -m pip install -r requirements.txt

dev:
	@test -f app.py || (printf '%s\n' "Run this from the beeline directory (where app.py lives), e.g.: cd beeline && make dev" >&2; exit 1)
	@echo "Open http://127.0.0.1:$(PORT)/   (Ctrl+C to stop)"
	$(PYTHON) -m uvicorn app:app --host 127.0.0.1 --port $(PORT) --reload

# One-time: git init  →  git remote add origin https://github.com/oxysub/beeline.git
#   →  make push
push:
	@test -d .git || (printf 'No .git in this folder. Run: git init && git add . && git commit -m "Initial"\n' >&2; exit 1)
	@git remote get-url $(REMOTE) >/dev/null 2>&1 || (printf 'No remote %s. Add: git remote add %s <your-repo-url> (e.g. https://github.com/oxysub/beeline.git)\n' '$(REMOTE)' '$(REMOTE)' >&2; exit 1)
	git push -u $(REMOTE) $(BRANCH)

# Optional manual deploy. Copy the "Deploy hook" URL from:
#   Render → your service → Settings → Build & deploy → Deploy hook
# Add to beeline/.env:  RENDER_DEPLOY_HOOK=https://api.render.com/deploy/srv-...
render:
	@set -a; \
	if [ -f "$(CURDIR)/.env" ]; then . "$(CURDIR)/.env"; fi; \
	set +a; \
	if [ -z "$$RENDER_DEPLOY_HOOK" ]; then \
	  printf 'Set RENDER_DEPLOY_HOOK in the environment or in .env (see Makefile comment above).\n' >&2; \
	  exit 1; \
	fi; \
	curl -fsS -X POST "$$RENDER_DEPLOY_HOOK" && printf '\nRender: deploy requested.\n'

# Push then trigger a Render deploy (hook). If the service is set to "Auto-Deploy" on
# `make push` alone is often enough; use release when you need the hook.
release: push render
