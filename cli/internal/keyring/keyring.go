package keyring

import (
	"fmt"
	"os"
	"path/filepath"

	gokeyring "github.com/zalando/go-keyring"

	"github.com/ashwathstephen/sairo/cli/internal/config"
)

const serviceName = "sairo-cli"

// Set stores a token for the given profile in the OS keyring.
// Falls back to a file-based store if the keyring is unavailable.
func Set(profile, token string) error {
	err := gokeyring.Set(serviceName, profile, token)
	if err == nil {
		return nil
	}
	// Fallback to file
	return setFile(profile, token)
}

// Get retrieves a token for the given profile.
// Checks SAIRO_TOKEN env var first, then OS keyring, then file fallback.
func Get(profile string) (string, error) {
	// Env var takes priority (for CI/CD)
	if tok := os.Getenv("SAIRO_TOKEN"); tok != "" {
		return tok, nil
	}

	tok, err := gokeyring.Get(serviceName, profile)
	if err == nil && tok != "" {
		return tok, nil
	}
	// Fallback to file
	return getFile(profile)
}

// Delete removes the stored token for a profile.
func Delete(profile string) error {
	_ = gokeyring.Delete(serviceName, profile)
	return deleteFile(profile)
}

// credentialsDir returns the path to the file-based credential store.
func credentialsDir() string {
	return filepath.Join(config.Dir(), "credentials")
}

func setFile(profile, token string) error {
	dir := credentialsDir()
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	p := filepath.Join(dir, profile)
	return os.WriteFile(p, []byte(token), 0600)
}

func getFile(profile string) (string, error) {
	p := filepath.Join(credentialsDir(), profile)
	data, err := os.ReadFile(p)
	if err != nil {
		return "", fmt.Errorf("not logged in (run: sairo login)")
	}
	return string(data), nil
}

func deleteFile(profile string) error {
	p := filepath.Join(credentialsDir(), profile)
	if err := os.Remove(p); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}
