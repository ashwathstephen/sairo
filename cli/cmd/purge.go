package cmd

import (
	"fmt"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var (
	purgeForce     bool
	purgeRecursive bool
)

var purgeCmd = &cobra.Command{
	Use:   "purge <bucket>/<key-or-prefix>",
	Short: "Permanently delete all versions and delete markers",
	Long: `Purge all versions of an object or all objects under a prefix.
This is irreversible — all version history is permanently deleted.

Examples:
  sairo purge my-bucket/old-file.log --force
  sairo purge my-bucket/old-data/ -r --force`,
	Args: cobra.ExactArgs(1),
	RunE: runPurge,
}

func init() {
	purgeCmd.Flags().BoolVarP(&purgeForce, "force", "f", false, "Skip confirmation")
	purgeCmd.Flags().BoolVarP(&purgeRecursive, "recursive", "r", false, "Purge all objects under prefix")
	rootCmd.AddCommand(purgeCmd)
}

func runPurge(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	bucket, key := parseBucketPath(args[0])

	if !purgeForce {
		return fmt.Errorf("purge is destructive — use --force to confirm")
	}

	var payload map[string]interface{}
	if purgeRecursive {
		payload = map[string]interface{}{
			"prefix": key,
		}
	} else {
		if key == "" {
			return fmt.Errorf("object key is required (use -r for recursive purge)")
		}
		payload = map[string]interface{}{
			"keys": []string{key},
		}
	}

	var resp struct {
		Purged int `json:"purged"`
		Errors int `json:"errors"`
	}
	if err := c.Post(fmt.Sprintf("/api/buckets/%s/purge-versions", bucket), payload, &resp); err != nil {
		return err
	}

	if flagJSON {
		return output.JSON(resp)
	}

	output.Success("Purged %d versions", resp.Purged)
	return nil
}
