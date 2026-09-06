package wyze

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
)

// Client talks to the Cryze Python API to get camera info and tokens.
// We reuse the existing Python API rather than reimplementing Wyze auth.
type Client struct {
	baseURL    string
	httpClient *http.Client
}

type DeviceInfo struct {
	CameraID   string `json:"cameraId"`
	StreamName string `json:"streamName"`
	LanIP      string `json:"lanIp"`
}

type AccessCredential struct {
	AccessID    string `json:"accessId"`
	AccessToken string `json:"accessToken"`
}

func NewClient(baseURL string) *Client {
	return &Client{
		baseURL:    baseURL,
		httpClient: &http.Client{Timeout: 30_000_000_000}, // 30s
	}
}

func (c *Client) GetCameraList() ([]string, error) {
	resp, err := c.httpClient.Get(c.baseURL + "/Camera/CameraList")
	if err != nil {
		return nil, fmt.Errorf("GET /Camera/CameraList: %w", err)
	}
	defer resp.Body.Close()

	var cameras []string
	if err := json.NewDecoder(resp.Body).Decode(&cameras); err != nil {
		return nil, fmt.Errorf("decode camera list: %w", err)
	}
	return cameras, nil
}

func (c *Client) GetDeviceInfo(deviceID string) (*DeviceInfo, error) {
	u := fmt.Sprintf("%s/Camera/DeviceInfo?deviceId=%s", c.baseURL, url.QueryEscape(deviceID))
	resp, err := c.httpClient.Get(u)
	if err != nil {
		return nil, fmt.Errorf("GET /Camera/DeviceInfo: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("device info %s: HTTP %d: %s", deviceID, resp.StatusCode, string(body))
	}

	var info DeviceInfo
	if err := json.NewDecoder(resp.Body).Decode(&info); err != nil {
		return nil, fmt.Errorf("decode device info: %w", err)
	}
	return &info, nil
}

func (c *Client) GetCameraToken(deviceID string) (*AccessCredential, error) {
	u := fmt.Sprintf("%s/Camera/CameraToken?deviceId=%s", c.baseURL, url.QueryEscape(deviceID))
	resp, err := c.httpClient.Get(u)
	if err != nil {
		return nil, fmt.Errorf("GET /Camera/CameraToken: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("camera token %s: HTTP %d: %s", deviceID, resp.StatusCode, string(body))
	}

	var cred AccessCredential
	if err := json.NewDecoder(resp.Body).Decode(&cred); err != nil {
		return nil, fmt.Errorf("decode camera token: %w", err)
	}
	return &cred, nil
}

// ReportUnreachable tells wyze-api that this camera stopped responding at its
// known LAN IP, so it can re-resolve the IP by MAC (arp-scan) before the next
// reconnect attempt. Best-effort: callers should log failures, not abort on them.
func (c *Client) ReportUnreachable(deviceID string) error {
	body, err := json.Marshal(map[string]string{"cameraId": deviceID})
	if err != nil {
		return fmt.Errorf("marshal report body: %w", err)
	}

	resp, err := c.httpClient.Post(c.baseURL+"/Camera/ReportUnreachable", "application/json", bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("POST /Camera/ReportUnreachable: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		respBody, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("report unreachable %s: HTTP %d: %s", deviceID, resp.StatusCode, string(respBody))
	}
	return nil
}
