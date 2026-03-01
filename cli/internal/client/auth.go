package client

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// LoginRequest represents the login API payload.
type LoginRequest struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

// LoginResponse represents the login API response.
type LoginResponse struct {
	Username    string `json:"username"`
	Role        string `json:"role"`
	Requires2FA bool   `json:"requires_2fa"`
}

// TwoFARequest represents the 2FA verification payload.
type TwoFARequest struct {
	Code string `json:"code"`
}

// MeResponse represents the /api/auth/me response.
type MeResponse struct {
	Username    string      `json:"username"`
	Role        string      `json:"role"`
	TOTPEnabled bool        `json:"totp_enabled"`
	ExpiresAt   interface{} `json:"expires_at"` // may be string (ISO) or number (epoch)
}

// ExpiresAtString returns expires_at as an ISO string regardless of API format.
func (m *MeResponse) ExpiresAtString() string {
	switch v := m.ExpiresAt.(type) {
	case string:
		return v
	case float64:
		return time.Unix(int64(v), 0).Format(time.RFC3339)
	default:
		return ""
	}
}

// Login authenticates with username and password.
// Returns the JWT token from the Set-Cookie header.
func (c *Client) Login(username, password string) (*LoginResponse, string, error) {
	resp, err := c.Do("POST", "/api/auth/login", strings.NewReader(
		fmt.Sprintf(`{"username":%q,"password":%q}`, username, password),
	))
	if err != nil {
		return nil, "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		return nil, "", parseAPIError(resp)
	}

	var loginResp LoginResponse
	if err := json.NewDecoder(resp.Body).Decode(&loginResp); err != nil {
		return nil, "", fmt.Errorf("failed to parse login response: %w", err)
	}

	// Extract JWT from Set-Cookie
	token := extractCookieToken(resp)

	return &loginResp, token, nil
}

// Verify2FA completes the second factor of authentication.
func (c *Client) Verify2FA(code string) (*LoginResponse, string, error) {
	resp, err := c.Do("POST", "/api/auth/2fa/verify", strings.NewReader(
		fmt.Sprintf(`{"code":%q}`, code),
	))
	if err != nil {
		return nil, "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		return nil, "", parseAPIError(resp)
	}

	var loginResp LoginResponse
	if err := json.NewDecoder(resp.Body).Decode(&loginResp); err != nil {
		return nil, "", fmt.Errorf("failed to parse 2FA response: %w", err)
	}

	token := extractCookieToken(resp)
	return &loginResp, token, nil
}

// Me returns the current user info.
func (c *Client) Me() (*MeResponse, error) {
	var me MeResponse
	if err := c.Get("/api/auth/me", nil, &me); err != nil {
		return nil, err
	}
	return &me, nil
}

// Refresh refreshes the JWT session.
func (c *Client) Refresh() (string, error) {
	resp, err := c.Do("POST", "/api/auth/refresh", nil)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		return "", parseAPIError(resp)
	}

	return extractCookieToken(resp), nil
}

// Logout invalidates the session.
func (c *Client) Logout() error {
	return c.DoJSON("POST", "/api/auth/logout", nil, nil)
}

// IsTokenExpiringSoon checks if a JWT is within 10% of its expiry.
func IsTokenExpiringSoon(token string) bool {
	claims, err := parseJWTClaims(token)
	if err != nil {
		return true // can't parse → treat as expiring
	}
	exp, ok := claims["exp"].(float64)
	if !ok {
		return true
	}
	iat, ok := claims["iat"].(float64)
	if !ok {
		// No iat, check if less than 1 hour left
		return time.Until(time.Unix(int64(exp), 0)) < 1*time.Hour
	}
	totalDuration := exp - iat
	remaining := exp - float64(time.Now().Unix())
	return remaining < totalDuration*0.1
}

// IsTokenExpired checks if a JWT has expired.
func IsTokenExpired(token string) bool {
	claims, err := parseJWTClaims(token)
	if err != nil {
		return true
	}
	exp, ok := claims["exp"].(float64)
	if !ok {
		return true
	}
	return time.Now().Unix() > int64(exp)
}

// TokenExpiresAt returns the expiry time of a JWT.
func TokenExpiresAt(token string) (time.Time, error) {
	claims, err := parseJWTClaims(token)
	if err != nil {
		return time.Time{}, err
	}
	exp, ok := claims["exp"].(float64)
	if !ok {
		return time.Time{}, fmt.Errorf("no exp claim")
	}
	return time.Unix(int64(exp), 0), nil
}

// parseJWTClaims decodes the claims from a JWT without validation.
// We only need to read expiry — the server validates the signature.
func parseJWTClaims(token string) (map[string]interface{}, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return nil, fmt.Errorf("invalid JWT format")
	}
	// Pad base64
	payload := parts[1]
	if m := len(payload) % 4; m != 0 {
		payload += strings.Repeat("=", 4-m)
	}
	data, err := base64.URLEncoding.DecodeString(payload)
	if err != nil {
		return nil, err
	}
	var claims map[string]interface{}
	if err := json.Unmarshal(data, &claims); err != nil {
		return nil, err
	}
	return claims, nil
}

// extractCookieToken extracts the access_token from Set-Cookie headers.
func extractCookieToken(resp interface{ Cookies() []*http.Cookie }) string {
	for _, c := range resp.Cookies() {
		if c.Name == "access_token" {
			return c.Value
		}
	}
	return ""
}
