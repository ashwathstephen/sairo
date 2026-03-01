package cmd

import (
	"fmt"
	"net/url"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var (
	diffSince string
	diffType  string
)

var diffCmd = &cobra.Command{
	Use:   "diff <bucket>/<prefix>",
	Short: "Show what changed in a prefix over a time period",
	Long: `Display storage changes using Sairo's history data.

Examples:
  sairo diff my-bucket/logs/ --since 24h
  sairo diff my-bucket/events/ --since 7d`,
	Args: cobra.ExactArgs(1),
	RunE: runDiff,
}

func init() {
	diffCmd.Flags().StringVarP(&diffSince, "since", "s", "24h", "Time window (e.g., 1h, 7d, 30d)")
	diffCmd.Flags().StringVarP(&diffType, "type", "t", "all", "Filter: added, removed, modified")
	rootCmd.AddCommand(diffCmd)
}

type storageHistoryResp struct {
	Prefix  string         `json:"prefix"`
	History []historyEntry `json:"history"`
}

type historyEntry struct {
	Timestamp   string `json:"timestamp"`
	ObjectCount int64  `json:"object_count"`
	TotalSize   int64  `json:"total_size"`
}

func runDiff(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	bucket, prefix := parseBucketPath(args[0])

	// Parse --since into days
	days, err := parseSinceDuration(diffSince)
	if err != nil {
		return err
	}

	params := url.Values{
		"days": {fmt.Sprintf("%d", days)},
	}
	if prefix != "" {
		params.Set("prefix", prefix)
	}

	var resp storageHistoryResp
	if err := c.Get(fmt.Sprintf("/api/buckets/%s/storage-history", bucket), params, &resp); err != nil {
		return err
	}

	if flagJSON {
		return output.JSON(resp)
	}

	if len(resp.History) < 2 {
		fmt.Println("Not enough history data for comparison.")
		return nil
	}

	oldest := resp.History[0]
	newest := resp.History[len(resp.History)-1]

	objDiff := newest.ObjectCount - oldest.ObjectCount
	sizeDiff := newest.TotalSize - oldest.TotalSize

	fmt.Printf("\n  Changes in %s/%s (last %s):\n\n", bucket, prefix, diffSince)

	// Object count change
	if objDiff > 0 {
		fmt.Printf("  %s files  (%s)\n", output.Green(fmt.Sprintf("+%s", output.FormatNumber(objDiff))),
			output.Green("+"+output.HumanSize(sizeDiff)))
	} else if objDiff < 0 {
		fmt.Printf("  %s files  (%s)\n", output.Red(fmt.Sprintf("%s", output.FormatNumber(objDiff))),
			output.Red(output.HumanSize(sizeDiff)))
	} else {
		fmt.Println("  No changes detected.")
	}

	// Net summary
	if sizeDiff != 0 {
		pctChange := ""
		if oldest.TotalSize > 0 {
			pct := float64(sizeDiff) / float64(oldest.TotalSize) * 100
			if pct > 0 {
				pctChange = fmt.Sprintf(" (+%.1f%%)", pct)
			} else {
				pctChange = fmt.Sprintf(" (%.1f%%)", pct)
			}
		}
		fmt.Printf("\n  Net: %s%s\n", formatSizeDiff(sizeDiff), pctChange)
	}

	fmt.Println()
	return nil
}

func formatSizeDiff(diff int64) string {
	if diff > 0 {
		return output.Green("+" + output.HumanSize(diff))
	}
	if diff < 0 {
		return output.Red("-" + output.HumanSize(-diff))
	}
	return "0 B"
}

func parseSinceDuration(s string) (int, error) {
	s = strings.TrimSpace(strings.ToLower(s))
	if s == "" {
		return 1, nil
	}

	// Try Go duration format first
	if d, err := time.ParseDuration(s); err == nil {
		days := int(d.Hours() / 24)
		if days < 1 {
			days = 1
		}
		return days, nil
	}

	// Try Xd format
	if strings.HasSuffix(s, "d") {
		var days int
		if _, err := fmt.Sscanf(s, "%dd", &days); err == nil {
			return days, nil
		}
	}

	return 0, fmt.Errorf("invalid duration: %s (use format like 24h, 7d, 30d)", s)
}
