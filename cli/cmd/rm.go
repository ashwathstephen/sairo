package cmd

import (
	"bufio"
	"fmt"
	"net/url"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/client"
	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var (
	rmRecursive     bool
	rmForce         bool
	rmPurgeVersions bool
	rmDryRun        bool
)

var rmCmd = &cobra.Command{
	Use:   "rm <bucket>/<key>",
	Short: "Delete objects",
	Long: `Delete a single object or recursively delete a prefix.

Examples:
  sairo rm my-bucket/tmp/old-file.log
  sairo rm my-bucket/logs/2025/ -r
  sairo rm my-bucket/old-data/ -r --purge-versions --force`,
	Args: cobra.ExactArgs(1),
	RunE: runRm,
}

func init() {
	rmCmd.Flags().BoolVarP(&rmRecursive, "recursive", "r", false, "Delete all objects under prefix")
	rmCmd.Flags().BoolVarP(&rmForce, "force", "f", false, "Skip confirmation prompt")
	rmCmd.Flags().BoolVar(&rmPurgeVersions, "purge-versions", false, "Also delete all old versions")
	rmCmd.Flags().BoolVar(&rmDryRun, "dry-run", false, "Show what would be deleted")
	rootCmd.AddCommand(rmCmd)
}

func runRm(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	bucket, key := parseBucketPath(args[0])

	if rmRecursive {
		return rmRecursiveDelete(c, bucket, key)
	}

	if key == "" {
		return fmt.Errorf("object key is required (use -r for recursive delete)")
	}

	if rmDryRun {
		fmt.Printf("Would delete: %s/%s\n", bucket, key)
		return nil
	}

	payload := map[string]interface{}{
		"keys": []string{key},
	}
	var resp map[string]interface{}
	if err := c.DeleteWithBody(fmt.Sprintf("/api/buckets/%s/objects", bucket), payload, &resp); err != nil {
		return err
	}

	if flagJSON {
		return output.JSON(resp)
	}

	output.Success("Deleted %s/%s", bucket, key)
	return nil
}

func rmRecursiveDelete(c *client.Client, bucket, prefix string) error {
	// Safety: require --force for purge-versions
	if rmPurgeVersions && !rmForce {
		return fmt.Errorf("--purge-versions requires --force (this permanently deletes all versions)")
	}

	if rmDryRun {
		fmt.Printf("Would recursively delete: %s/%s\n", bucket, prefix)
		if rmPurgeVersions {
			fmt.Println("  (including all object versions)")
		}
		return nil
	}

	// Get count for confirmation
	if !rmForce {
		var sizeResp struct {
			ObjectCount int64 `json:"object_count"`
			TotalSize   int64 `json:"total_size"`
		}
		params := url.Values{}
		if prefix != "" {
			params.Set("prefix", prefix)
		}
		_ = c.Get(fmt.Sprintf("/api/buckets/%s/folder-size", bucket), params, &sizeResp)

		fmt.Printf("This will delete %s objects (%s) under %s/%s\n",
			output.FormatNumber(sizeResp.ObjectCount),
			output.HumanSize(sizeResp.TotalSize),
			bucket, prefix)
		fmt.Print("Continue? [y/N]: ")

		reader := bufio.NewReader(os.Stdin)
		answer, _ := reader.ReadString('\n')
		answer = strings.TrimSpace(strings.ToLower(answer))
		if answer != "y" && answer != "yes" {
			fmt.Println("Cancelled.")
			os.Exit(5)
		}
	}

	payload := map[string]interface{}{
		"prefix":         prefix,
		"purge_versions": rmPurgeVersions,
	}
	var resp struct {
		Deleted int    `json:"deleted"`
		Errors  int    `json:"errors"`
		Prefix  string `json:"prefix"`
	}
	if err := c.DeleteWithBody(fmt.Sprintf("/api/buckets/%s/folder", bucket), payload, &resp); err != nil {
		return err
	}

	if flagJSON {
		return output.JSON(resp)
	}

	output.Success("Deleted %d objects under %s/%s", resp.Deleted, bucket, prefix)
	return nil
}
