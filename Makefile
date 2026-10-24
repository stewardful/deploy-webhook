.DEFAULT_GOAL := check
export GOWORK := off

.PHONY: check test build smoke
check:
	@test -z "$$(gofmt -l cmd notify)"
	go vet ./...

test:
	go test -race -count=1 ./...

build:
	CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o bin/release-notify ./cmd/release-notify

smoke: build
	python3 tests/smoke.py "$(CURDIR)/bin/release-notify"
