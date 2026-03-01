package cmd

import (
	"bufio"
	"fmt"
	"net/url"
	"os"
	"strings"

	"github.com/spf13/cobra"
	"golang.org/x/term"

	"github.com/ashwathstephen/sairo/cli/internal/client"
	"github.com/ashwathstephen/sairo/cli/internal/config"
	"github.com/ashwathstephen/sairo/cli/internal/keyring"
	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var (
	loginToken    string
	loginProfile  string
	loginUsername string
	loginPassword string
)

var loginCmd = &cobra.Command{
	Use:   "login [url]",
	Short: "Authenticate with a Sairo instance",
	Long: `Authenticate with a Sairo server and store credentials securely.

Examples:
  sairo login https://sairo.example.com
  sairo login https://sairo.example.com --token sairo_abc123...
  sairo login https://sairo.example.com --profile staging`,
	Args: cobra.MaximumNArgs(1),
	RunE: runLogin,
}

func init() {
	loginCmd.Flags().StringVar(&loginToken, "token", "", "Use an API token instead of username/password")
	loginCmd.Flags().StringVar(&loginProfile, "profile", "", "Save as named profile (default: derived from hostname)")
	loginCmd.Flags().StringVarP(&loginUsername, "username", "u", "", "Username (avoids interactive prompt)")
	loginCmd.Flags().StringVar(&loginPassword, "password", "", "Password (avoids interactive prompt; prefer stdin for security)")
	rootCmd.AddCommand(loginCmd)
}

func runLogin(cmd *cobra.Command, args []string) error {
	// Get URL
	var serverURL string
	if len(args) > 0 {
		serverURL = args[0]
	} else {
		// Try to use current profile's URL
		if p := resolveProfile(); p != "" {
			if prof, err := config.LoadProfile(p); err == nil {
				serverURL = prof.URL
			}
		}
		if serverURL == "" {
			return fmt.Errorf("server URL is required: sairo login <url>")
		}
	}

	// Validate URL
	if !strings.HasPrefix(serverURL, "http://") && !strings.HasPrefix(serverURL, "https://") {
		serverURL = "https://" + serverURL
	}
	if !isValidURL(serverURL) {
		return fmt.Errorf("invalid URL: %s", serverURL)
	}

	// Derive profile name
	profileName := loginProfile
	if profileName == "" {
		u, _ := url.Parse(serverURL)
		profileName = u.Hostname()
		// Strip common prefixes
		profileName = strings.TrimPrefix(profileName, "sairo.")
		profileName = strings.TrimPrefix(profileName, "sairo-")
	}

	if loginToken != "" {
		return loginWithToken(serverURL, profileName)
	}
	return loginWithPassword(serverURL, profileName)
}

func loginWithToken(serverURL, profileName string) error {
	token := loginToken
	if token == "-" {
		// Read from stdin
		scanner := bufio.NewScanner(os.Stdin)
		if scanner.Scan() {
			token = strings.TrimSpace(scanner.Text())
		}
	}

	c := client.New(serverURL, token, "bearer", "", flagDebug)
	me, err := c.Me()
	if err != nil {
		return fmt.Errorf("authentication failed: %w", err)
	}

	// Store
	if err := keyring.Set(profileName, token); err != nil {
		return fmt.Errorf("failed to store credentials: %w", err)
	}
	if err := config.SaveProfile(profileName, config.Profile{URL: serverURL}); err != nil {
		return fmt.Errorf("failed to save profile: %w", err)
	}
	if err := config.SetCurrentProfile(profileName); err != nil {
		return fmt.Errorf("failed to set current profile: %w", err)
	}

	if flagJSON {
		return output.JSON(map[string]string{
			"username": me.Username,
			"role":     me.Role,
			"profile":  profileName,
			"method":   "token",
		})
	}
	output.Success("Authenticated via API token (role: %s)", me.Role)
	fmt.Printf("  Profile saved: %s\n", profileName)
	return nil
}

func loginWithPassword(serverURL, profileName string) error {
	reader := bufio.NewReader(os.Stdin)

	username := loginUsername
	password := loginPassword

	// Interactive prompts if flags not provided
	if username == "" {
		fmt.Print("Username: ")
		username, _ = reader.ReadString('\n')
		username = strings.TrimSpace(username)
		if username == "" {
			return fmt.Errorf("username is required")
		}
	}

	if password == "" {
		fmt.Print("Password: ")
		if term.IsTerminal(int(os.Stdin.Fd())) {
			passBytes, err := term.ReadPassword(int(os.Stdin.Fd()))
			fmt.Println()
			if err != nil {
				return fmt.Errorf("failed to read password: %w", err)
			}
			password = string(passBytes)
		} else {
			// Non-interactive: read from stdin
			password, _ = reader.ReadString('\n')
			password = strings.TrimSpace(password)
		}
	}

	// Login
	c := client.New(serverURL, "", "", "", flagDebug)
	loginResp, token, err := c.Login(username, password)
	if err != nil {
		return fmt.Errorf("login failed: %w", err)
	}

	// Handle 2FA
	if loginResp.Requires2FA {
		// Use the pending token for 2FA
		c.Token = token
		c.TokenType = "cookie"

		fmt.Print("2FA code: ")
		code, _ := reader.ReadString('\n')
		code = strings.TrimSpace(code)

		twoFAResp, newToken, err := c.Verify2FA(code)
		if err != nil {
			return fmt.Errorf("2FA verification failed: %w", err)
		}
		loginResp = twoFAResp
		token = newToken
	}

	if token == "" {
		return fmt.Errorf("server did not return a session token")
	}

	// Store credentials
	if err := keyring.Set(profileName, token); err != nil {
		return fmt.Errorf("failed to store credentials: %w", err)
	}
	if err := config.SaveProfile(profileName, config.Profile{URL: serverURL}); err != nil {
		return fmt.Errorf("failed to save profile: %w", err)
	}
	if err := config.SetCurrentProfile(profileName); err != nil {
		return fmt.Errorf("failed to set current profile: %w", err)
	}

	if flagJSON {
		return output.JSON(map[string]string{
			"username": loginResp.Username,
			"role":     loginResp.Role,
			"profile":  profileName,
			"method":   "password",
		})
	}
	output.Success("Logged in as %s (role: %s)", loginResp.Username, loginResp.Role)
	fmt.Printf("  Profile saved: %s\n", profileName)
	return nil
}
