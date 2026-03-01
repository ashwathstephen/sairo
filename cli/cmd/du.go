package cmd

import (
	"fmt"
	"net/url"
	"sort"
	"strings"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var (
	duDepth int
	duSort  string
	duTop   int
)

var duCmd = &cobra.Command{
	Use:   "du [bucket[/prefix]]",
	Short: "Show disk usage (like du -sh)",
	Long: `Show storage usage breakdown using Sairo's pre-computed folder stats.

Examples:
  sairo du my-bucket
  sairo du my-bucket/logs/ -d 2
  sairo du my-bucket --sort name --top 10`,
	Args: cobra.ExactArgs(1),
	RunE: runDu,
}

func init() {
	duCmd.Flags().IntVarP(&duDepth, "depth", "d", 1, "Folder depth for breakdown")
	duCmd.Flags().StringVarP(&duSort, "sort", "s", "size", "Sort by: size, name, count")
	duCmd.Flags().IntVarP(&duTop, "top", "n", 0, "Show only top N entries")
	rootCmd.AddCommand(duCmd)
}

type storageBreakdownResp struct {
	Prefix      string           `json:"prefix"`
	TotalSize   int64            `json:"total_size"`
	ObjectCount int64            `json:"object_count"`
	Children    []storageChild   `json:"children"`
}

type storageChild struct {
	Prefix      string `json:"prefix"`
	Name        string `json:"name"`
	ObjectCount int64  `json:"object_count"`
	TotalSize   int64  `json:"total_size"`
}

func runDu(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	bucket, prefix := parseBucketPath(args[0])

	params := url.Values{}
	if prefix != "" {
		params.Set("prefix", prefix)
	}

	var resp storageBreakdownResp
	if err := c.Get(fmt.Sprintf("/api/buckets/%s/storage-breakdown", bucket), params, &resp); err != nil {
		return err
	}

	// Sort children
	sortChildren(resp.Children)

	// Limit
	if duTop > 0 && len(resp.Children) > duTop {
		resp.Children = resp.Children[:duTop]
	}

	if flagJSON {
		return output.JSON(resp)
	}

	tbl := output.NewTable("PREFIX", "OBJECTS", "SIZE", "%")
	for _, child := range resp.Children {
		tbl.AddRow(
			child.Name,
			output.FormatNumber(child.ObjectCount),
			output.HumanSize(child.TotalSize),
			output.Percentage(child.TotalSize, resp.TotalSize),
		)
	}
	// Total row
	if output.IsTTY() && len(resp.Children) > 1 {
		sep := strings.Repeat("─", 60)
		tbl.AddRow(output.Dim(sep), "", "", "")
		tbl.AddRow(
			output.Bold("TOTAL"),
			output.FormatNumber(resp.ObjectCount),
			output.HumanSize(resp.TotalSize),
			"100%",
		)
	}
	tbl.Render()
	return nil
}

func sortChildren(children []storageChild) {
	switch strings.ToLower(duSort) {
	case "name":
		sort.Slice(children, func(i, j int) bool {
			return children[i].Name < children[j].Name
		})
	case "count":
		sort.Slice(children, func(i, j int) bool {
			return children[i].ObjectCount > children[j].ObjectCount
		})
	default: // size
		sort.Slice(children, func(i, j int) bool {
			return children[i].TotalSize > children[j].TotalSize
		})
	}
}
