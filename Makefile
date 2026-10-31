SHELL := /bin/sh
PYTHON ?= python3
GO ?= go
GOARCH ?= amd64

.PHONY: build-host build-web test-host test-web test-runtime

build-host:
	mkdir -p build
	cd host && CGO_ENABLED=0 GOOS=linux GOARCH=$(GOARCH) $(GO) build -trimpath -o ../build/personal-bot-gateway ./cmd/personal-bot-gateway
	cd host && CGO_ENABLED=0 GOOS=linux GOARCH=$(GOARCH) $(GO) build -trimpath -o ../build/personal-bot-helper ./cmd/personal-bot-helper
	cd host && CGO_ENABLED=0 GOOS=linux GOARCH=$(GOARCH) $(GO) build -trimpath -o ../build/personal-botctl ./cmd/personal-botctl
	CGO_ENABLED=0 GOOS=linux GOARCH=$(GOARCH) $(GO) build -trimpath -o build/personal-bot-tls deploy/tls-proxy.go

build-web:
	cd web && npm ci && npm run build

test-host:
	cd host && $(GO) test ./...

test-web:
	cd web && npm ci && npm run typecheck && npm run lint && npm test && npm run build

test-runtime:
	PYTHONPATH=runtime $(PYTHON) -m pytest -q runtime/tests
