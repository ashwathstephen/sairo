package cmd

import (
	"fmt"
	"net/url"
	"strings"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var versioningCmd = &cobra.Command{
	Use:   "versioning <bucket> [enable|suspend|status]",
	Short: "View or change bucket versioning",
	Long: `Check or modify bucket versioning status.

Examples:
  sairo versioning my-bucket
  sairo versioning my-bucket enable
  sairo versioning my-bucket suspend`,
	Args: cobra.RangeArgs(1, 2),
	RunE: runVersioning,
}

func init() {
	rootCmd.AddCommand(versioningCmd)
}

func runVersioning(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	bucket := args[0]
	action := "status"
	if len(args) > 1 {
		action = strings.ToLower(args[1])
	}

	switch action {
	case "status", "get":
		var resp struct {
			Status    string `json:"status"`
			MFADelete string `json:"mfa_delete"`
		}
		if err := c.Get(fmt.Sprintf("/api/buckets/%s/versioning", bucket), nil, &resp); err != nil {
			return err
		}
		if flagJSON {
			return output.JSON(resp)
		}
		fmt.Printf("Versioning: %s\n", resp.Status)
		return nil

	case "enable":
		params := url.Values{"enabled": {"true"}}
		var resp map[string]interface{}
		if err := c.Put(fmt.Sprintf("/api/buckets/%s/versioning?%s", bucket, params.Encode()), nil, &resp); err != nil {
			return err
		}
		if flagJSON {
			return output.JSON(resp)
		}
		output.Success("Versioning enabled on %s", bucket)
		return nil

	case "suspend":
		params := url.Values{"enabled": {"false"}}
		var resp map[string]interface{}
		if err := c.Put(fmt.Sprintf("/api/buckets/%s/versioning?%s", bucket, params.Encode()), nil, &resp); err != nil {
			return err
		}
		if flagJSON {
			return output.JSON(resp)
		}
		output.Success("Versioning suspended on %s", bucket)
		return nil

	default:
		return fmt.Errorf("unknown action: %s (use enable, suspend, or status)", action)
	}
}
