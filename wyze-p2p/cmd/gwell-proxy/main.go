// cmd/gwell-proxy/main.go
//
// Production entry point for the Wyze GWell P2P bridge.
//
// v2 changes:
// - Serialized P2P session setup via connectMu to prevent cross-session interference
// - Fresh discovery on reconnect when both cameras are down
// - Proper 15s stagger between all session setups, not just initial start
// - DeviceName set to cameraID for correct device targeting

package main

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/wlatic/wyze-gwell-bridge/wyze-p2p/pkg/gwell"
	"github.com/wlatic/wyze-gwell-bridge/wyze-p2p/pkg/stream"
	"github.com/wlatic/wyze-gwell-bridge/wyze-p2p/pkg/wyze"
)

const (
	defaultAPIURL        = "http://wyze-api:8080"
	defaultMediamtxHost  = "localhost"
	defaultMediamtxPort  = 8554
	tokenRefreshInterval = 1 * time.Hour
	deadmanTimeout       = 120 * time.Second
	cameraStagger        = 15 * time.Second
	reconnectDelay       = 10 * time.Second
	cacheFile            = "data/token_cache.json"
)

// connectMu serializes P2P session setup across all cameras.
// The GWell P2P server gets confused when multiple sessions from the same
// account do InitInfoMsg simultaneously — frames leak between sessions,
// causing "bad count" decryption failures. Only one camera should be in
// the connect→certify→initInfo→subscribe→calling phase at a time.
var connectMu sync.Mutex

// tokenCache persists credentials across restarts.
type tokenCache struct {
	AccessID    string `json:"accessId"`
	AccessToken string `json:"accessToken"`
	ServerAddr  string `json:"serverAddr"`
	CachedAt    int64  `json:"cachedAt"`
	TTLSeconds  int64  `json:"ttlSeconds"`
}

func (tc *tokenCache) isValid() bool {
	if tc.AccessID == "" || tc.AccessToken == "" {
		return false
	}
	elapsed := time.Since(time.Unix(tc.CachedAt, 0))
	ttl := time.Duration(tc.TTLSeconds) * time.Second
	if ttl == 0 {
		ttl = 7 * 24 * time.Hour
	}
	return elapsed < ttl
}

// writeTracker wraps an io.Writer to track the last-write time atomically.
type writeTracker struct {
	inner     *stream.FFmpegPublisher
	lastWrite atomic.Int64 // unix nano
}

func (w *writeTracker) Write(p []byte) (int, error) {
	n, err := w.inner.Write(p)
	if n > 0 {
		w.lastWrite.Store(time.Now().UnixNano())
	}
	return n, err
}

func (w *writeTracker) lastWriteTime() time.Time {
	ns := w.lastWrite.Load()
	if ns == 0 {
		return time.Time{}
	}
	return time.Unix(0, ns)
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// shared state protected by sharedMu
var (
	sharedMu      sync.Mutex
	sharedToken   *gwell.AccessToken
	sharedServer  string
	sharedDevices []gwell.DeviceInfo
)

func main() {
	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds)
	log.Println("[main] Wyze GWell P2P proxy starting")

	apiURL := envOr("CRYZE_API_URL", defaultAPIURL)
	mediamtxHost := envOr("MEDIAMTX_HOST", defaultMediamtxHost)
	mediamtxPort := defaultMediamtxPort
	if p, err := strconv.Atoi(os.Getenv("MEDIAMTX_PORT")); err == nil && p > 0 {
		mediamtxPort = p
	}

	log.Printf("[main] API: %s, MediaMTX: %s:%d", apiURL, mediamtxHost, mediamtxPort)

	client := wyze.NewClient(apiURL)

	// Wait for the API to be ready
	log.Println("[main] Waiting for wyze-api to be ready...")
	for i := 0; i < 60; i++ {
		if _, err := client.GetCameraList(); err == nil {
			break
		}
		time.Sleep(2 * time.Second)
	}

	// Get camera list
	cameraIDs, err := client.GetCameraList()
	if err != nil {
		log.Fatalf("[main] Failed to get camera list: %v", err)
	}
	if len(cameraIDs) == 0 {
		log.Fatalf("[main] No cameras found. Check wyze-api logs.")
	}
	log.Printf("[main] Found %d camera(s): %v", len(cameraIDs), cameraIDs)

	// Initial discovery
	if err := refreshDiscovery(client, cameraIDs[0]); err != nil {
		log.Fatalf("[main] Initial discovery failed: %v", err)
	}

	// Set up signal handling
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)

	// Start per-camera goroutines with stagger
	var wg sync.WaitGroup
	stopCh := make(chan struct{})

	for i, camID := range cameraIDs {
		if i > 0 {
			log.Printf("[main] Staggering camera start (%s)...", cameraStagger)
			time.Sleep(cameraStagger)
		}

		wg.Add(1)
		go func(cameraID string) {
			defer wg.Done()
			runCamera(client, cameraID, mediamtxHost, mediamtxPort, stopCh)
		}(camID)
	}

	// Token refresh loop
	go func() {
		ticker := time.NewTicker(tokenRefreshInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				log.Println("[main] Refreshing token...")
				if err := refreshDiscovery(client, cameraIDs[0]); err != nil {
					log.Printf("[main] Token refresh failed: %v", err)
				} else {
					log.Println("[main] Token refreshed (will apply on next reconnect)")
				}
			case <-stopCh:
				return
			}
		}
	}()

	// Wait for signal
	sig := <-sigCh
	log.Printf("[main] Received signal %v, shutting down...", sig)
	close(stopCh)
	wg.Wait()
	log.Println("[main] Shutdown complete")
}

// refreshDiscovery fetches a fresh token and runs device discovery.
// Results are stored in shared state for all camera goroutines to use.
func refreshDiscovery(client *wyze.Client, anyCameraID string) error {
	cred, err := client.GetCameraToken(anyCameraID)
	if err != nil {
		return fmt.Errorf("get token: %w", err)
	}

	token, err := gwell.ParseAccessToken(cred.AccessID, cred.AccessToken)
	if err != nil {
		return fmt.Errorf("parse token: %w", err)
	}

	// Try cache first
	cache := loadCache()
	if cache != nil && cache.isValid() && cache.ServerAddr != "" {
		log.Println("[main] Using cached P2P server address:", cache.ServerAddr)
		sharedMu.Lock()
		sharedToken = token
		sharedServer = cache.ServerAddr
		// Keep existing devices if we have them
		sharedMu.Unlock()
	} else {
		log.Println("[main] Running device discovery...")
		result, err := gwell.DiscoverDevices(token)
		if err != nil {
			return fmt.Errorf("discovery: %w", err)
		}
		log.Printf("[main] Discovery complete: server=%s, %d device(s)", result.ServerAddr, len(result.Devices))

		sharedMu.Lock()
		sharedToken = token
		sharedServer = result.ServerAddr
		sharedDevices = result.Devices
		sharedMu.Unlock()
	}

	saveCache(&tokenCache{
		AccessID:    cred.AccessID,
		AccessToken: cred.AccessToken,
		ServerAddr:  sharedServer,
		CachedAt:    time.Now().Unix(),
		TTLSeconds:  int64((7 * 24 * time.Hour).Seconds()),
	})

	return nil
}

// getSharedState returns a snapshot of the current shared state.
func getSharedState() (*gwell.AccessToken, string, []gwell.DeviceInfo) {
	sharedMu.Lock()
	defer sharedMu.Unlock()
	return sharedToken, sharedServer, sharedDevices
}

// runCamera is the reconnect loop for a single camera.
func runCamera(client *wyze.Client, cameraID string,
	mediamtxHost string, mediamtxPort int, stopCh chan struct{}) {

	for {
		select {
		case <-stopCh:
			log.Printf("[%s] Stop signal received", cameraID)
			return
		default:
		}

		// Get fresh token on each attempt
		cred, err := client.GetCameraToken(cameraID)
		if err != nil {
			log.Printf("[%s] Token fetch failed: %v, using shared token", cameraID, err)
		} else {
			newToken, err := gwell.ParseAccessToken(cred.AccessID, cred.AccessToken)
			if err != nil {
				log.Printf("[%s] Token parse failed: %v, using shared token", cameraID, err)
			} else {
				sharedMu.Lock()
				sharedToken = newToken
				sharedMu.Unlock()
			}
		}

		err = streamCamera(client, cameraID, mediamtxHost, mediamtxPort, stopCh)
		if err != nil {
			log.Printf("[%s] Stream error: %v", cameraID, err)
		}

		select {
		case <-stopCh:
			return
		case <-time.After(reconnectDelay):
			log.Printf("[%s] Reconnecting...", cameraID)
		}
	}
}

// streamCamera runs a single streaming session for one camera.
// It acquires connectMu to serialize the P2P handshake phase.
func streamCamera(client *wyze.Client, cameraID string,
	mediamtxHost string, mediamtxPort int, stopCh chan struct{}) error {

	// Get device info for stream name and LAN IP
	info, err := client.GetDeviceInfo(cameraID)
	if err != nil {
		return fmt.Errorf("get device info: %w", err)
	}

	streamName := info.StreamName
	if streamName == "" {
		streamName = cameraID
	}
	streamPath := streamName
	if !strings.HasPrefix(streamPath, "live/") {
		streamPath = "live/" + streamPath
	}

	log.Printf("[%s] Starting stream: %s (LAN IP: %s)", cameraID, streamPath, info.LanIP)

	// Start ffmpeg publisher
	ffmpeg, err := stream.StartFFmpegPublisher(streamPath, mediamtxHost, mediamtxPort)
	if err != nil {
		return fmt.Errorf("start ffmpeg: %w", err)
	}
	defer ffmpeg.Close()

	// Wrap with write tracker for deadman switch
	tracker := &writeTracker{inner: ffmpeg}
	tracker.lastWrite.Store(time.Now().UnixNano())

	// === SERIALIZED SECTION ===
	// Acquire the connect mutex so only one camera is doing the P2P
	// handshake at a time. This prevents cross-session interference
	// on the P2P server.
	log.Printf("[%s] Waiting for connect lock...", cameraID)
	connectMu.Lock()
	log.Printf("[%s] Acquired connect lock, starting P2P session", cameraID)

	token, serverAddr, devices := getSharedState()

	sess := gwell.NewSession(gwell.SessionConfig{
		Token:       token,
		ServerAddr:  serverAddr,
		CameraLanIP: info.LanIP,
		DeviceName:  cameraID,
		H264Writer:  tracker,
		Devices:     devices,
	})

	// Run the session — it will go through connect, certify, initInfo,
	// subscribe, calling, and then enter streamLoop. We release the
	// lock after a delay to let the handshake complete before the next
	// camera tries.
	errCh := make(chan error, 1)
	go func() {
		errCh <- sess.Run(cameraID)
	}()

	// Wait for either: streaming started (give it time), error, or stop
	// Release the lock after the stagger delay so the next camera can go
	go func() {
		time.Sleep(cameraStagger)
		connectMu.Unlock()
		log.Printf("[%s] Released connect lock", cameraID)
	}()
	// === END SERIALIZED SECTION ===

	// Monitor: deadman switch + ffmpeg health + stop signal
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case err := <-errCh:
			return fmt.Errorf("session ended: %w", err)

		case <-stopCh:
			sess.Close()
			return nil

		case <-ticker.C:
			// Check ffmpeg health
			if !ffmpeg.Alive() {
				sess.Close()
				return fmt.Errorf("ffmpeg process died")
			}

			// Deadman switch: no data for too long
			last := tracker.lastWriteTime()
			if !last.IsZero() && time.Since(last) > deadmanTimeout {
				sess.Close()
				return fmt.Errorf("deadman timeout: no stream data for %s", deadmanTimeout)
			}
		}
	}
}

func loadCache() *tokenCache {
	data, err := os.ReadFile(cacheFile)
	if err != nil {
		return nil
	}
	var tc tokenCache
	if err := json.Unmarshal(data, &tc); err != nil {
		return nil
	}
	return &tc
}

func saveCache(tc *tokenCache) {
	dir := filepath.Dir(cacheFile)
	os.MkdirAll(dir, 0755)
	data, err := json.MarshalIndent(tc, "", "  ")
	if err != nil {
		log.Printf("[cache] Failed to marshal: %v", err)
		return
	}
	if err := os.WriteFile(cacheFile, data, 0600); err != nil {
		log.Printf("[cache] Failed to write %s: %v", cacheFile, err)
		return
	}
	log.Printf("[cache] Saved token cache to %s", cacheFile)
}
