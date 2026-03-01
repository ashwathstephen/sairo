package client

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/hashicorp/go-retryablehttp"
)

// ExitCode constants for structured error handling.
const (
	ExitOK          = 0
	ExitGeneral     = 1
	ExitAuth        = 2
	ExitForbidden   = 3
	ExitNotFound    = 4
	ExitCancelled   = 5
)

// APIError represents an error response from the Sairo API.
type APIError struct {
	StatusCode int
	Detail     string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("%s (HTTP %d)", e.Detail, e.StatusCode)
}

// ExitCode returns the appropriate exit code for this error.
func (e *APIError) ExitCode() int {
	switch {
	case e.StatusCode == 401:
		return ExitAuth
	case e.StatusCode == 403:
		return ExitForbidden
	case e.StatusCode == 404:
		return ExitNotFound
	default:
		return ExitGeneral
	}
}

// Client is the HTTP client for talking to a Sairo server.
type Client struct {
	BaseURL    string
	Token      string
	TokenType  string // "bearer" or "cookie"
	EndpointID string
	Debug      bool
	HTTP       *http.Client
}

// New creates a new Sairo API client.
func New(baseURL, token, tokenType, endpointID string, debug bool) *Client {
	retryClient := retryablehttp.NewClient()
	retryClient.RetryMax = 3
	retryClient.RetryWaitMin = 1 * time.Second
	retryClient.RetryWaitMax = 4 * time.Second
	retryClient.Logger = nil // suppress retryablehttp logs
	retryClient.CheckRetry = func(ctx context.Context, resp *http.Response, err error) (bool, error) {
		if err != nil {
			return true, nil
		}
		// Only retry 5xx
		if resp.StatusCode >= 500 {
			return true, nil
		}
		return false, nil
	}

	return &Client{
		BaseURL:    strings.TrimRight(baseURL, "/"),
		Token:      token,
		TokenType:  tokenType,
		EndpointID: endpointID,
		Debug:      debug,
		HTTP:       retryClient.StandardClient(),
	}
}

// apiURL builds a full URL for an API path, handling endpoint routing.
func (c *Client) apiURL(path string) string {
	if c.EndpointID != "" && c.EndpointID != "default" {
		// Rewrite /api/... → /api/e/{endpoint_id}/...
		if strings.HasPrefix(path, "/api/") {
			path = "/api/e/" + c.EndpointID + path[4:]
		}
	}
	return c.BaseURL + path
}

// newRequest creates an authenticated HTTP request.
func (c *Client) newRequest(method, path string, body io.Reader) (*http.Request, error) {
	u := c.apiURL(path)
	req, err := http.NewRequest(method, u, body)
	if err != nil {
		return nil, err
	}

	// Auth
	if c.Token != "" {
		if c.TokenType == "bearer" || strings.HasPrefix(c.Token, "sairo_") {
			req.Header.Set("Authorization", "Bearer "+c.Token)
		} else {
			req.AddCookie(&http.Cookie{Name: "access_token", Value: c.Token})
		}
	}

	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	return req, nil
}

// Do executes a request and returns the raw response.
func (c *Client) Do(method, path string, body io.Reader) (*http.Response, error) {
	req, err := c.newRequest(method, path, body)
	if err != nil {
		return nil, err
	}

	start := time.Now()
	if c.Debug {
		fmt.Fprintf(os.Stderr, "→ %s %s\n", method, c.apiURL(path))
	}

	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, fmt.Errorf("request failed: %w", err)
	}

	if c.Debug {
		elapsed := time.Since(start)
		fmt.Fprintf(os.Stderr, "← %d (%s)\n", resp.StatusCode, elapsed.Round(time.Millisecond))
	}

	return resp, nil
}

// DoJSON executes a request and decodes the JSON response into v.
func (c *Client) DoJSON(method, path string, body io.Reader, v interface{}) error {
	resp, err := c.Do(method, path, body)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		return parseAPIError(resp)
	}

	if v != nil {
		return json.NewDecoder(resp.Body).Decode(v)
	}
	return nil
}

// Get is a convenience for DoJSON with GET.
func (c *Client) Get(path string, params url.Values, v interface{}) error {
	if len(params) > 0 {
		path += "?" + params.Encode()
	}
	return c.DoJSON("GET", path, nil, v)
}

// Post is a convenience for DoJSON with POST and a JSON body.
func (c *Client) Post(path string, payload interface{}, v interface{}) error {
	body, err := jsonBody(payload)
	if err != nil {
		return err
	}
	return c.DoJSON("POST", path, body, v)
}

// Put is a convenience for DoJSON with PUT and a JSON body.
func (c *Client) Put(path string, payload interface{}, v interface{}) error {
	body, err := jsonBody(payload)
	if err != nil {
		return err
	}
	return c.DoJSON("PUT", path, body, v)
}

// Delete is a convenience for DoJSON with DELETE.
func (c *Client) Delete(path string, v interface{}) error {
	return c.DoJSON("DELETE", path, nil, v)
}

// DeleteWithBody is a convenience for DoJSON with DELETE and a JSON body.
func (c *Client) DeleteWithBody(path string, payload interface{}, v interface{}) error {
	body, err := jsonBody(payload)
	if err != nil {
		return err
	}
	return c.DoJSON("DELETE", path, body, v)
}

// GetStream executes a GET and returns the raw response for streaming reads.
func (c *Client) GetStream(path string, params url.Values) (*http.Response, error) {
	if len(params) > 0 {
		path += "?" + params.Encode()
	}
	resp, err := c.Do("GET", path, nil)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		defer resp.Body.Close()
		return nil, parseAPIError(resp)
	}
	return resp, nil
}

// parseAPIError reads an error response body.
func parseAPIError(resp *http.Response) error {
	body, _ := io.ReadAll(resp.Body)
	var errResp struct {
		Detail string `json:"detail"`
	}
	if json.Unmarshal(body, &errResp) == nil && errResp.Detail != "" {
		return &APIError{StatusCode: resp.StatusCode, Detail: errResp.Detail}
	}
	return &APIError{StatusCode: resp.StatusCode, Detail: strings.TrimSpace(string(body))}
}

// jsonBody marshals a value into a reader.
func jsonBody(v interface{}) (io.Reader, error) {
	if v == nil {
		return nil, nil
	}
	data, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	return strings.NewReader(string(data)), nil
}

// MaskToken returns a masked version of a token for debug output.
func MaskToken(token string) string {
	if len(token) <= 8 {
		return "****"
	}
	return token[:8] + "****"
}
