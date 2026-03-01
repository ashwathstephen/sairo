package cmd

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var lifecycleFile string

var lifecycleCmd = &cobra.Command{
	Use:   "lifecycle <bucket> [get|set|delete]",
	Short: "View or manage lifecycle rules",
	Long: `Manage S3 bucket lifecycle rules.

Examples:
  sairo lifecycle my-bucket
  sairo lifecycle my-bucket set --file rules.json
  sairo lifecycle my-bucket delete`,
	Args: cobra.RangeArgs(1, 2),
	RunE: runLifecycle,
}

func init() {
	lifecycleCmd.Flags().StringVar(&lifecycleFile, "file", "", "JSON file containing lifecycle rules (for set)")
	rootCmd.AddCommand(lifecycleCmd)
}

type lifecycleResp struct {
	Rules []lifecycleRule `json:"rules"`
}

type lifecycleRule struct {
	ID             string `json:"id"`
	Status         string `json:"status"`
	Prefix         string `json:"prefix"`
	ExpirationDays *int   `json:"expiration_days"`
	NoncurrentDays *int   `json:"noncurrent_days"`
	AbortDays      *int   `json:"abort_days"`
}

func runLifecycle(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	bucket := args[0]
	action := "get"
	if len(args) > 1 {
		action = strings.ToLower(args[1])
	}

	switch action {
	case "get":
		var resp lifecycleResp
		if err := c.Get(fmt.Sprintf("/api/buckets/%s/lifecycle", bucket), nil, &resp); err != nil {
			return err
		}
		if flagJSON {
			return output.JSON(resp)
		}
		if len(resp.Rules) == 0 {
			fmt.Println("No lifecycle rules configured.")
			return nil
		}
		tbl := output.NewTable("RULE ID", "PREFIX", "EXPIRATION", "NONCURRENT", "STATUS")
		for _, r := range resp.Rules {
			exp := "-"
			if r.ExpirationDays != nil {
				exp = fmt.Sprintf("%d days", *r.ExpirationDays)
			}
			nonc := "-"
			if r.NoncurrentDays != nil {
				nonc = fmt.Sprintf("%d days", *r.NoncurrentDays)
			}
			tbl.AddRow(r.ID, r.Prefix, exp, nonc, r.Status)
		}
		tbl.Render()
		return nil

	case "set":
		if lifecycleFile == "" {
			return fmt.Errorf("--file is required for set (JSON file with lifecycle rules)")
		}
		data, err := os.ReadFile(lifecycleFile)
		if err != nil {
			return fmt.Errorf("failed to read %s: %w", lifecycleFile, err)
		}
		var payload map[string]interface{}
		if err := json.Unmarshal(data, &payload); err != nil {
			return fmt.Errorf("invalid JSON in %s: %w", lifecycleFile, err)
		}
		var resp map[string]interface{}
		if err := c.Put(fmt.Sprintf("/api/buckets/%s/lifecycle", bucket), payload, &resp); err != nil {
			return err
		}
		if flagJSON {
			return output.JSON(resp)
		}
		output.Success("Lifecycle rules updated")
		return nil

	case "delete":
		var resp map[string]interface{}
		if err := c.Delete(fmt.Sprintf("/api/buckets/%s/lifecycle", bucket), &resp); err != nil {
			return err
		}
		if flagJSON {
			return output.JSON(resp)
		}
		output.Success("Lifecycle rules deleted")
		return nil

	default:
		return fmt.Errorf("unknown action: %s (use get, set, or delete)", action)
	}
}
