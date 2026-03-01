package cmd

import (
	"fmt"
	"net/url"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/client"
	"github.com/ashwathstephen/sairo/cli/internal/config"
	"github.com/ashwathstephen/sairo/cli/internal/keyring"
	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var (
	cliVersion = "dev"
	cliCommit  = "none"
	cliDate    = "unknown"

	// Global flags
	flagJSON     bool
	flagProfile  string
	flagEndpoint string
	flagQuiet    bool
	flagNoColor  bool
	flagDebug    bool
)

// SetVersionInfo is called from main to inject build-time values.
func SetVersionInfo(version, commit, date string) {
	cliVersion = version
	cliCommit = commit
	cliDate = date
}

var rootCmd = &cobra.Command{
	Use:   "sairo",
	Short: "Fast S3 storage browser CLI",
	Long:  "Sairo CLI — sub-second queries on petabyte-scale S3 buckets.\nPowered by Sairo's indexed backend for instant search, listing, and analytics.",
	PersistentPreRun: func(cmd *cobra.Command, args []string) {
		if flagNoColor {
			os.Setenv("NO_COLOR", "1")
		}
		// Auto-enable JSON when stdout is not a TTY
		if !output.IsTTY() && !cmd.Flags().Changed("json") {
			flagJSON = true
		}
	},
	SilenceUsage:  true,
	SilenceErrors: true,
}

// version subcommand
var versionCmd = &cobra.Command{
	Use:   "version",
	Short: "Print version information",
	Run: func(cmd *cobra.Command, args []string) {
		if flagJSON {
			output.JSON(map[string]string{
				"version": cliVersion,
				"commit":  cliCommit,
				"date":    cliDate,
			})
			return
		}
		fmt.Printf("sairo %s (commit: %s, built: %s)\n", cliVersion, cliCommit, cliDate)
	},
}

func init() {
	config.Load()

	rootCmd.PersistentFlags().BoolVarP(&flagJSON, "json", "j", false, "Output as JSON")
	rootCmd.PersistentFlags().StringVarP(&flagProfile, "profile", "p", "", "Use a specific login profile")
	rootCmd.PersistentFlags().StringVarP(&flagEndpoint, "endpoint", "e", "", "Route through a specific S3 endpoint")
	rootCmd.PersistentFlags().BoolVarP(&flagQuiet, "quiet", "q", false, "Suppress non-essential output")
	rootCmd.PersistentFlags().BoolVar(&flagNoColor, "no-color", false, "Disable colored output")
	rootCmd.PersistentFlags().BoolVar(&flagDebug, "debug", false, "Print HTTP request/response details")

	rootCmd.AddCommand(versionCmd)
}

// Execute runs the root command.
func Execute() error {
	if err := rootCmd.Execute(); err != nil {
		// Map API errors to exit codes
		if apiErr, ok := err.(*client.APIError); ok {
			output.Error("%s", apiErr.Detail)
			os.Exit(apiErr.ExitCode())
		}
		output.Error("%s", err)
		return err
	}
	return nil
}

// resolveProfile determines the active profile name.
func resolveProfile() string {
	if flagProfile != "" {
		return flagProfile
	}
	if p := config.CurrentProfile(); p != "" {
		return p
	}
	return ""
}

// newClient creates an authenticated API client for the current profile.
func newClient() (*client.Client, error) {
	profile := resolveProfile()
	if profile == "" {
		return nil, fmt.Errorf("not logged in (run: sairo login <url>)")
	}

	prof, err := config.LoadProfile(profile)
	if err != nil {
		return nil, err
	}

	token, err := keyring.Get(profile)
	if err != nil {
		return nil, err
	}

	// Determine token type
	tokenType := "cookie"
	if strings.HasPrefix(token, "sairo_") {
		tokenType = "bearer"
	}

	// Check token expiry and refresh if needed
	if tokenType == "cookie" && !client.IsTokenExpired(token) && client.IsTokenExpiringSoon(token) {
		c := client.New(prof.URL, token, tokenType, resolveEndpoint(prof), flagDebug)
		if newToken, err := c.Refresh(); err == nil && newToken != "" {
			token = newToken
			_ = keyring.Set(profile, token)
		}
	}

	if tokenType == "cookie" && client.IsTokenExpired(token) {
		return nil, fmt.Errorf("session expired (run: sairo login)")
	}

	endpoint := resolveEndpoint(prof)
	return client.New(prof.URL, token, tokenType, endpoint, flagDebug), nil
}

// resolveEndpoint determines the S3 endpoint to use.
func resolveEndpoint(prof *config.Profile) string {
	if flagEndpoint != "" {
		return flagEndpoint
	}
	if prof.Endpoint != "" {
		return prof.Endpoint
	}
	return ""
}

// parseBucketPath splits "bucket/prefix/path" into bucket and prefix.
func parseBucketPath(path string) (bucket, prefix string) {
	path = strings.TrimPrefix(path, "/")
	idx := strings.IndexByte(path, '/')
	if idx < 0 {
		return path, ""
	}
	return path[:idx], path[idx+1:]
}

// requireArg ensures at least one argument is provided.
func requireArg(args []string, name string) (string, error) {
	if len(args) < 1 {
		return "", fmt.Errorf("%s is required", name)
	}
	return args[0], nil
}

// isValidURL checks if a string is a valid HTTP(S) URL.
func isValidURL(s string) bool {
	u, err := url.Parse(s)
	if err != nil {
		return false
	}
	return u.Scheme == "http" || u.Scheme == "https"
}
