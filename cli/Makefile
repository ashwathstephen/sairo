VERSION ?= dev
COMMIT  := $(shell git rev-parse --short HEAD 2>/dev/null || echo "none")
DATE    := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)
LDFLAGS := -s -w -X main.version=$(VERSION) -X main.commit=$(COMMIT) -X main.date=$(DATE)

.PHONY: build clean test vet snapshot install

build:
	CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o sairo .

install:
	CGO_ENABLED=0 go install -ldflags "$(LDFLAGS)" .

clean:
	rm -f sairo

test:
	go test ./...

vet:
	go vet ./...

snapshot:
	goreleaser build --snapshot --clean
