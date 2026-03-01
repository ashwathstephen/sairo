package cmd

import (
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strings"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/client"
	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var (
	lsLong      bool
	lsRecursive bool
	lsSort      string
	lsReverse   bool
	lsFresh     bool
	lsLimit     int
)

var lsCmd = &cobra.Command{
	Use:   "ls [bucket[/prefix]]",
	Short: "List buckets, folders, or files",
	Long: `List buckets (no args), or folders and files at a path.
Uses Sairo's indexed backend for sub-second responses.

Examples:
  sairo ls
  sairo ls my-bucket/
  sairo ls my-bucket/logs/2026-03/ -l
  sairo ls my-bucket/ -r --sort size --reverse`,
	Args: cobra.MaximumNArgs(1),
	RunE: runLs,
}

func init() {
	lsCmd.Flags().BoolVarP(&lsLong, "long", "l", false, "Show size, date, and details")
	lsCmd.Flags().BoolVarP(&lsRecursive, "recursive", "r", false, "List all objects under prefix")
	lsCmd.Flags().StringVarP(&lsSort, "sort", "s", "name", "Sort by: name, size, date")
	lsCmd.Flags().BoolVar(&lsReverse, "reverse", false, "Reverse sort order")
	lsCmd.Flags().BoolVar(&lsFresh, "fresh", false, "Bypass index, query S3 directly")
	lsCmd.Flags().IntVarP(&lsLimit, "limit", "n", 0, "Limit number of results (0 = unlimited)")
	rootCmd.AddCommand(lsCmd)
}

func runLs(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	if len(args) == 0 {
		return listBuckets(c)
	}

	bucket, prefix := parseBucketPath(args[0])
	return listObjects(c, bucket, prefix)
}

// bucketsResp matches the /api/buckets response.
type bucketsResp struct {
	Buckets []bucketListEntry `json:"buckets"`
}

type bucketListEntry struct {
	Name        string `json:"name"`
	Created     string `json:"created"`
	IndexStatus string `json:"index_status"`
	ObjectCount int64  `json:"object_count"`
	TotalSize   int64  `json:"total_size"`
	Permission  string `json:"permission"`
}

func listBuckets(c *client.Client) error {
	var resp bucketsResp
	if err := c.Get("/api/buckets", nil, &resp); err != nil {
		return err
	}

	if flagJSON {
		return output.JSON(resp.Buckets)
	}

	if len(resp.Buckets) == 0 {
		fmt.Println("No buckets found.")
		return nil
	}

	tbl := output.NewTable("BUCKET", "OBJECTS", "SIZE", "INDEXED")
	for _, b := range resp.Buckets {
		indexed := output.Dim("-")
		if b.IndexStatus == "ready" {
			indexed = output.Green("✓")
		} else if b.IndexStatus == "crawling" {
			indexed = output.Yellow("crawling")
		} else if b.IndexStatus != "" {
			indexed = b.IndexStatus
		}
		tbl.AddRow(
			b.Name,
			output.FormatNumber(b.ObjectCount),
			output.HumanSize(b.TotalSize),
			indexed,
		)
	}
	tbl.Render()
	return nil
}

func listObjects(c *client.Client, bucket, prefix string) error {
	params := url.Values{}
	if prefix != "" {
		params.Set("prefix", prefix)
	}
	if lsFresh {
		params.Set("fresh", "true")
	}

	resp, err := c.GetStream(fmt.Sprintf("/api/buckets/%s/list", bucket), params)
	if err != nil {
		return err
	}

	var allFolders []client.FolderEntry
	var allFiles []client.FileEntry

	if lsRecursive {
		// Recursively collect all files under prefix
		err = collectAllFiles(c, bucket, prefix, resp, &allFiles)
	} else {
		err = client.StreamList(resp, func(page client.ListPage) error {
			allFolders = append(allFolders, page.Folders...)
			allFiles = append(allFiles, page.Files...)
			return nil
		})
	}
	if err != nil {
		return err
	}

	// Sort
	sortFiles(allFiles)

	// Limit
	if lsLimit > 0 && len(allFiles) > lsLimit {
		allFiles = allFiles[:lsLimit]
	}

	if flagJSON {
		if lsRecursive {
			return output.JSON(map[string]interface{}{
				"files": allFiles,
				"count": len(allFiles),
			})
		}
		return output.JSON(map[string]interface{}{
			"folders": allFolders,
			"files":   allFiles,
		})
	}

	if lsLong {
		return renderLongListing(allFolders, allFiles)
	}
	return renderShortListing(allFolders, allFiles)
}

// collectAllFiles recursively walks through all subfolders to collect every file.
func collectAllFiles(c *client.Client, bucket, prefix string, resp *http.Response, files *[]client.FileEntry) error {
	folders, pageFiles, err := client.StreamListCollect(resp)
	if err != nil {
		return err
	}
	*files = append(*files, pageFiles...)

	for _, folder := range folders {
		params := url.Values{"prefix": {folder.Prefix}}
		if lsFresh {
			params.Set("fresh", "true")
		}
		subResp, err := c.GetStream(fmt.Sprintf("/api/buckets/%s/list", bucket), params)
		if err != nil {
			return err
		}
		if err := collectAllFiles(c, bucket, folder.Prefix, subResp, files); err != nil {
			return err
		}
	}
	return nil
}

func renderShortListing(folders []client.FolderEntry, files []client.FileEntry) error {
	for _, f := range folders {
		fmt.Println(output.Cyan(f.Name))
	}
	for _, f := range files {
		fmt.Println(f.Name)
	}
	if output.IsTTY() && !flagQuiet {
		var totalSize int64
		for _, f := range files {
			totalSize += f.Size
		}
		summary := fmt.Sprintf("\n  %d folders, %d files", len(folders), len(files))
		if totalSize > 0 {
			summary += fmt.Sprintf(" (%s)", output.HumanSize(totalSize))
		}
		fmt.Println(output.Dim(summary))
	}
	return nil
}

func renderLongListing(folders []client.FolderEntry, files []client.FileEntry) error {
	if lsRecursive {
		tbl := output.NewTable("KEY", "SIZE", "MODIFIED")
		for _, f := range files {
			modified := "-"
			if t, err := output.ParseTime(f.LastModified); err == nil {
				modified = output.HumanTime(t)
			}
			tbl.AddRow(f.Key, output.HumanSize(f.Size), modified)
		}
		tbl.Render()
	} else {
		tbl := output.NewTable("TYPE", "NAME", "SIZE", "MODIFIED")
		for _, f := range folders {
			tbl.AddRow(output.Cyan("d/"), output.Cyan(f.Name), "-", "-")
		}
		for _, f := range files {
			modified := "-"
			if t, err := output.ParseTime(f.LastModified); err == nil {
				modified = output.HumanTime(t)
			}
			tbl.AddRow("f", f.Name, output.HumanSize(f.Size), modified)
		}
		tbl.Render()
	}

	if output.IsTTY() && !flagQuiet {
		var totalSize int64
		for _, f := range files {
			totalSize += f.Size
		}
		summary := fmt.Sprintf("\n  %d folders, %d files", len(folders), len(files))
		if totalSize > 0 {
			summary += fmt.Sprintf(" (%s)", output.HumanSize(totalSize))
		}
		fmt.Println(output.Dim(summary))
	}
	return nil
}

func sortFiles(files []client.FileEntry) {
	switch strings.ToLower(lsSort) {
	case "size":
		sort.Slice(files, func(i, j int) bool {
			if lsReverse {
				return files[i].Size > files[j].Size
			}
			return files[i].Size < files[j].Size
		})
	case "date":
		sort.Slice(files, func(i, j int) bool {
			if lsReverse {
				return files[i].LastModified > files[j].LastModified
			}
			return files[i].LastModified < files[j].LastModified
		})
	default: // name
		sort.Slice(files, func(i, j int) bool {
			a, b := files[i].Name, files[j].Name
			if a == "" {
				a = files[i].Key
			}
			if b == "" {
				b = files[j].Key
			}
			if lsReverse {
				return a > b
			}
			return a < b
		})
	}
}
