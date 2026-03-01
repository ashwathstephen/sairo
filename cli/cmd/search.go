package cmd

import (
	"fmt"
	"net/url"
	"time"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var (
	searchPrefix string
	searchLimit  int
	searchLong   bool
)

var searchCmd = &cobra.Command{
	Use:   "search <bucket> <query>",
	Short: "Full-text search across indexed bucket contents",
	Long: `Search for files by name using Sairo's FTS5 index.
Returns results in sub-second regardless of bucket size.

Examples:
  sairo search my-bucket "access.log"
  sairo search my-bucket "*.parquet" --prefix logs/2026-03/ -l`,
	Args: cobra.ExactArgs(2),
	RunE: runSearch,
}

func init() {
	searchCmd.Flags().StringVar(&searchPrefix, "prefix", "", "Restrict search to a prefix")
	searchCmd.Flags().IntVarP(&searchLimit, "limit", "n", 200, "Maximum results")
	searchCmd.Flags().BoolVarP(&searchLong, "long", "l", false, "Show size and date")
	rootCmd.AddCommand(searchCmd)
}

type searchResp struct {
	Results []searchResult `json:"results"`
	Count   int            `json:"count"`
	Query   string         `json:"query"`
}

type searchResult struct {
	Key          string `json:"key"`
	Size         int64  `json:"size"`
	LastModified string `json:"last_modified"`
}

func runSearch(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	bucket := args[0]
	query := args[1]

	params := url.Values{}
	params.Set("q", query)
	if searchPrefix != "" {
		params.Set("prefix", searchPrefix)
	}
	params.Set("limit", fmt.Sprintf("%d", searchLimit))

	start := time.Now()
	var resp searchResp
	if err := c.Get(fmt.Sprintf("/api/buckets/%s/search", bucket), params, &resp); err != nil {
		return err
	}
	elapsed := time.Since(start)

	if flagJSON {
		return output.JSON(resp)
	}

	if resp.Count == 0 {
		fmt.Println("No results found.")
		return nil
	}

	if searchLong {
		tbl := output.NewTable("KEY", "SIZE", "MODIFIED")
		for _, r := range resp.Results {
			modified := "-"
			if t, err := output.ParseTime(r.LastModified); err == nil {
				modified = output.HumanTime(t)
			}
			tbl.AddRow(r.Key, output.HumanSize(r.Size), modified)
		}
		tbl.Render()
	} else {
		for _, r := range resp.Results {
			fmt.Println(r.Key)
		}
	}

	if output.IsTTY() && !flagQuiet {
		fmt.Printf("\n  %s results in %s\n", output.FormatNumber(int64(resp.Count)), elapsed.Round(time.Millisecond))
	}

	return nil
}
