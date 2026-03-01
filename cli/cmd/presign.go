package cmd

import (
	"fmt"
	"net/url"
	"time"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var (
	presignExpires int
	presignVersion string
)

var presignCmd = &cobra.Command{
	Use:   "presign <bucket>/<key>",
	Short: "Generate a presigned URL for sharing",
	Long: `Generate a time-limited presigned URL for direct S3 access.

Examples:
  sairo presign my-bucket/reports/summary.pdf
  sairo presign my-bucket/data.csv --expires 86400
  sairo presign my-bucket/config.yaml --version abc123`,
	Args: cobra.ExactArgs(1),
	RunE: runPresign,
}

func init() {
	presignCmd.Flags().IntVar(&presignExpires, "expires", 3600, "Expiry in seconds (60-604800)")
	presignCmd.Flags().StringVarP(&presignVersion, "version", "v", "", "Presign a specific version")
	rootCmd.AddCommand(presignCmd)
}

func runPresign(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	bucket, key := parseBucketPath(args[0])
	if key == "" {
		return fmt.Errorf("object key is required")
	}

	params := url.Values{
		"key":     {key},
		"expires": {fmt.Sprintf("%d", presignExpires)},
	}

	endpoint := fmt.Sprintf("/api/buckets/%s/presigned-url", bucket)
	if presignVersion != "" {
		params.Set("version_id", presignVersion)
		endpoint = fmt.Sprintf("/api/buckets/%s/version-presigned-url", bucket)
	}

	var resp struct {
		URL       string `json:"url"`
		ExpiresIn int    `json:"expires_in"`
	}
	if err := c.Get(endpoint, params, &resp); err != nil {
		return err
	}

	if flagJSON {
		return output.JSON(resp)
	}

	fmt.Println(resp.URL)
	if output.IsTTY() && !flagQuiet {
		expiryTime := time.Now().Add(time.Duration(presignExpires) * time.Second)
		fmt.Printf("\n  Expires: %s (%s)\n", expiryTime.Format(time.RFC3339), output.HumanDuration(time.Duration(presignExpires)*time.Second))
	}
	return nil
}
