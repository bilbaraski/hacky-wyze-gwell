# Native Go P2P client for Wyze GWell cameras.
#
# Pure Go implementation — no QEMU shim, no Android libraries.
# All crypto (RC5, XOR, checksum) and protocol logic is native Go.
#
# Architecture:
#   Go binary (gwell-proxy) handles the entire P2P connection natively
#   ffmpeg pipes H.264 to mediamtx via RTSP

# ---- Stage 1: Build Go binary ----
FROM golang:1.21-alpine AS builder
WORKDIR /app
COPY go.mod ./
RUN go mod download 2>/dev/null || true
COPY cmd/ cmd/
COPY pkg/ pkg/
# Build for the native platform (was hardcoded GOARCH=amd64, which breaks arm64 hosts like a Pi). (upstream fix candidate)
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /gwell-proxy ./cmd/gwell-proxy/

# ---- Stage 2: Runtime image ----
FROM alpine:3.21

RUN apk add --no-cache ffmpeg ca-certificates

COPY --from=builder /gwell-proxy /app/gwell-proxy

ENV CRYZE_API_URL=http://wyze-api:8080
ENV MEDIAMTX_HOST=localhost
ENV MEDIAMTX_PORT=8554

ENTRYPOINT ["/app/gwell-proxy"]
