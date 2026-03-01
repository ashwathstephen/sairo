package cmd

import (
	"fmt"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/config"
	"github.com/ashwathstephen/sairo/cli/internal/keyring"
	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var logoutAll bool

var logoutCmd = &cobra.Command{
	Use:   "logout",
	Short: "Clear stored credentials and session",
	Long: `Log out from the current or specified profile.

Examples:
  sairo logout
  sairo logout --all
  sairo logout --profile staging`,
	RunE: runLogout,
}

func init() {
	logoutCmd.Flags().BoolVar(&logoutAll, "all", false, "Remove all profiles")
	rootCmd.AddCommand(logoutCmd)
}

func runLogout(cmd *cobra.Command, args []string) error {
	if logoutAll {
		profiles := config.ListProfiles()
		for _, p := range profiles {
			_ = keyring.Delete(p)
			_ = config.DeleteProfile(p)
		}
		_ = config.SetCurrentProfile("")
		if flagJSON {
			return output.JSON(map[string]interface{}{
				"logged_out": true,
				"profiles":   profiles,
			})
		}
		output.Success("Logged out from all profiles (%d)", len(profiles))
		return nil
	}

	profile := resolveProfile()
	if profile == "" {
		return fmt.Errorf("not logged in")
	}

	// Try to call server logout (best-effort)
	if c, err := newClient(); err == nil {
		_ = c.Logout()
	}

	_ = keyring.Delete(profile)

	current := config.CurrentProfile()
	if current == profile {
		_ = config.SetCurrentProfile("")
	}

	if flagJSON {
		return output.JSON(map[string]interface{}{
			"logged_out": true,
			"profile":    profile,
		})
	}
	output.Success("Logged out from %s", profile)
	return nil
}
