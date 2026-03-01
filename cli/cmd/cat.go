package cmd

import (
	"fmt"
	"net/url"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var (
	catHead int
	catTail int
	catMax  int
)

var catCmd = &cobra.Command{
	Use:   "cat <bucket>/<key>",
	Short: "Print object contents to stdout",
	Long: `Display file contents. Useful for piping config files and logs.

Examples:
  sairo cat my-bucket/config/app.yaml
  sairo cat my-bucket/logs/app.log --tail 1000
  sairo cat my-bucket/data.csv --head 5000 | head -50`,
	Args: cobra.ExactArgs(1),
	RunE: runCat,
}

func init() {
	catCmd.Flags().IntVar(&catHead, "head", 0, "Show first N bytes")
	catCmd.Flags().IntVar(&catTail, "tail", 0, "Show last N bytes")
	catCmd.Flags().IntVar(&catMax, "max", 5*1024*1024, "Max bytes to fetch (default 5MB)")
	rootCmd.AddCommand(catCmd)
}

type previewResp struct {
	Content     string `json:"content"`
	Truncated   bool   `json:"truncated"`
	ContentType string `json:"content_type"`
	Showing     string `json:"showing"`
	TotalSize   int64  `json:"total_size"`
}

func runCat(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	bucket, key := parseBucketPath(args[0])
	if key == "" {
		return fmt.Errorf("object key is required")
	}

	params := url.Values{"key": {key}}

	var resp previewResp

	if catTail > 0 {
		params.Set("max_bytes", fmt.Sprintf("%d", catTail))
		if err := c.Get(fmt.Sprintf("/api/buckets/%s/preview-tail", bucket), params, &resp); err != nil {
			return err
		}
	} else {
		maxBytes := catMax
		if catHead > 0 {
			maxBytes = catHead
		}
		params.Set("max_bytes", fmt.Sprintf("%d", maxBytes))
		if err := c.Get(fmt.Sprintf("/api/buckets/%s/preview", bucket), params, &resp); err != nil {
			return err
		}
	}

	// Cat always outputs raw content unless --json was explicitly passed
	if flagJSON && cmd.Flags().Changed("json") {
		return output.JSON(resp)
	}

	fmt.Print(resp.Content)

	// Warn if truncated (to stderr so it doesn't pollute piped output)
	if resp.Truncated && output.IsTTY() {
		output.Warn("output truncated (use --max to increase limit)")
	}

	return nil
}
