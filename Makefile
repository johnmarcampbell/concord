# Makefile — local Docker image build + container run for Concord.
#
# Thin convenience wrappers over the `docker build` / `docker run`
# invocations documented in docs/docker.md. Everything here is
# overridable on the command line, e.g.:
#
#     make run HOST_PORT=9000
#     make build IMAGE=concord:dev
#
# This is for *local* dev only; real deployments wire up ports, volumes,
# and TLS in compose (see docs/docker.md).

IMAGE     ?= concord
HOST_PORT ?= 8000
# Container always binds 0.0.0.0:8000 internally (the Dockerfile CMD); the
# host port is what `make run HOST_PORT=...` remaps.
CTR_PORT  := 8000
DATA_DIR  ?= $(CURDIR)/data
NAME      ?= concord

# Forward the API keys from the host environment only if they're set, so
# `make run` doesn't clobber them with empty values inside the container.
ENV_ARGS :=
ifdef OPENAI_API_KEY
ENV_ARGS += -e OPENAI_API_KEY
endif
ifdef CONGRESS_API_KEY
ENV_ARGS += -e CONGRESS_API_KEY
endif

.PHONY: build run stop shell logs

## build: build the local image (tagged $(IMAGE))
build:
	docker build -t $(IMAGE) .

## run: build then launch the web server, forwarding $(HOST_PORT) -> container 8000
## Keys: already-set environment wins; ./.env is only a fallback if present.
serve-container: build
	mkdir -p "$(DATA_DIR)"
	pre_openai="$${OPENAI_API_KEY:-}"; pre_congress="$${CONGRESS_API_KEY:-}"; \
	set -a; [ -f .env ] && . ./.env; set +a; \
	[ -n "$$pre_openai" ] && export OPENAI_API_KEY="$$pre_openai"; \
	[ -n "$$pre_congress" ] && export CONGRESS_API_KEY="$$pre_congress"; \
	docker run --rm \
		--name $(NAME) \
		-p $(HOST_PORT):$(CTR_PORT) \
		-v "$(DATA_DIR):/app/data" \
		$${OPENAI_API_KEY:+-e OPENAI_API_KEY} \
		$${CONGRESS_API_KEY:+-e CONGRESS_API_KEY} \
		$(IMAGE)

## stop: stop the running container (if any)
stop-container:
	-docker stop $(NAME)

## shell: open an interactive shell in a throwaway container
enter-container: build
	docker run --rm -it \
		-v "$(DATA_DIR):/app/data" \
		$(ENV_ARGS) \
		$(IMAGE) \
		/bin/bash

## logs: follow logs from the running container
logs-container:
	docker logs -f $(NAME)
