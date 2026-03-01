package cmd

import (
	"fmt"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var cpRecursive bool

var cpCmd = &cobra.Command{
	Use:   "cp <src-bucket>/<key> <dst-bucket>[/key]",
	Short: "Copy an object within or between buckets (server-side)",
	Long: `Server-side copy — no data is downloaded locally.

Examples:
  sairo cp my-bucket/config/app.yaml my-bucket/config/app.yaml.bak
  sairo cp prod-bucket/data.csv staging-bucket/data.csv`,
	Args: cobra.ExactArgs(2),
	RunE: runCp,
}

func init() {
	cpCmd.Flags().BoolVarP(&cpRecursive, "recursive", "r", false, "Copy all objects under prefix")
	rootCmd.AddCommand(cpCmd)
}

func runCp(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	srcBucket, srcKey := parseBucketPath(args[0])
	dstBucket, dstKey := parseBucketPath(args[1])
	if srcKey == "" {
		return fmt.Errorf("source key is required")
	}
	if dstKey == "" {
		dstKey = srcKey
	}

	payload := map[string]string{
		"source_key": srcKey,
		"dest_key":   dstKey,
	}
	if dstBucket != srcBucket {
		payload["dest_bucket"] = dstBucket
	}

	var resp map[string]interface{}
	if err := c.Post(fmt.Sprintf("/api/buckets/%s/copy", srcBucket), payload, &resp); err != nil {
		return err
	}

	if flagJSON {
		return output.JSON(resp)
	}

	output.Success("Copied to %s/%s", dstBucket, dstKey)
	return nil
}
