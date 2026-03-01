package cmd

import (
	"fmt"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var mvCmd = &cobra.Command{
	Use:   "mv <bucket>/<key> <bucket>/<new-key>",
	Short: "Rename or move an object (server-side copy + delete)",
	Long: `Move/rename an object within a bucket or across buckets.

Examples:
  sairo mv my-bucket/old-name.log my-bucket/new-name.log
  sairo mv prod-bucket/tmp/data.csv prod-bucket/archive/data.csv`,
	Args: cobra.ExactArgs(2),
	RunE: runMv,
}

func init() {
	rootCmd.AddCommand(mvCmd)
}

func runMv(cmd *cobra.Command, args []string) error {
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
		return fmt.Errorf("destination key is required")
	}

	if srcBucket == dstBucket {
		// Same bucket — use rename endpoint
		payload := map[string]string{
			"source_key": srcKey,
			"dest_key":   dstKey,
		}
		var resp map[string]interface{}
		if err := c.Post(fmt.Sprintf("/api/buckets/%s/rename", srcBucket), payload, &resp); err != nil {
			return err
		}
	} else {
		// Cross-bucket — copy then delete
		copyPayload := map[string]string{
			"source_key":  srcKey,
			"dest_key":    dstKey,
			"dest_bucket": dstBucket,
		}
		var copyResp map[string]interface{}
		if err := c.Post(fmt.Sprintf("/api/buckets/%s/copy", srcBucket), copyPayload, &copyResp); err != nil {
			return err
		}

		deletePayload := map[string]interface{}{
			"keys": []string{srcKey},
		}
		var delResp map[string]interface{}
		if err := c.DeleteWithBody(fmt.Sprintf("/api/buckets/%s/objects", srcBucket), deletePayload, &delResp); err != nil {
			return fmt.Errorf("copy succeeded but delete of source failed: %w", err)
		}
	}

	if flagJSON {
		return output.JSON(map[string]string{
			"from": srcBucket + "/" + srcKey,
			"to":   dstBucket + "/" + dstKey,
		})
	}

	output.Success("Moved to %s/%s", dstBucket, dstKey)
	return nil
}
