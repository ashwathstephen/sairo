package cmd

import (
	"fmt"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var restoreVersion string

var restoreCmd = &cobra.Command{
	Use:   "restore <bucket>/<key> --version <id>",
	Short: "Restore a specific version as the latest",
	Long: `Restore an older version of an object by copying it as the new latest.

Examples:
  sairo restore my-bucket/config/app.yaml --version abc123def456`,
	Args: cobra.ExactArgs(1),
	RunE: runRestore,
}

func init() {
	restoreCmd.Flags().StringVarP(&restoreVersion, "version", "v", "", "Version ID to restore (required)")
	restoreCmd.MarkFlagRequired("version")
	rootCmd.AddCommand(restoreCmd)
}

func runRestore(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	bucket, key := parseBucketPath(args[0])
	if key == "" {
		return fmt.Errorf("object key is required")
	}

	payload := map[string]string{
		"key":        key,
		"version_id": restoreVersion,
	}

	var resp map[string]interface{}
	if err := c.Post(fmt.Sprintf("/api/buckets/%s/version-restore", bucket), payload, &resp); err != nil {
		return err
	}

	if flagJSON {
		return output.JSON(resp)
	}

	output.Success("Restored version %s as latest", restoreVersion)
	return nil
}
