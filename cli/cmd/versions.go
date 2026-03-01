package cmd

import (
	"fmt"
	"net/url"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var versionsCmd = &cobra.Command{
	Use:   "versions <bucket>/<key>",
	Short: "List all versions of an object",
	Long: `Show all versions and delete markers for a specific object.

Examples:
  sairo versions my-bucket/config/app.yaml`,
	Args: cobra.ExactArgs(1),
	RunE: runVersions,
}

func init() {
	rootCmd.AddCommand(versionsCmd)
}

type objectVersionsResp struct {
	Key           string          `json:"key"`
	Versions      []versionEntry  `json:"versions"`
	DeleteMarkers []deleteMarker  `json:"delete_markers"`
}

type versionEntry struct {
	VersionID    string `json:"version_id"`
	Size         int64  `json:"size"`
	LastModified string `json:"last_modified"`
	IsLatest     bool   `json:"is_latest"`
	ETag         string `json:"etag"`
	StorageClass string `json:"storage_class"`
}

type deleteMarker struct {
	VersionID    string `json:"version_id"`
	LastModified string `json:"last_modified"`
	IsLatest     bool   `json:"is_latest"`
}

func runVersions(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	bucket, key := parseBucketPath(args[0])
	if key == "" {
		return fmt.Errorf("object key is required")
	}

	var resp objectVersionsResp
	if err := c.Get(fmt.Sprintf("/api/buckets/%s/object-versions", bucket), url.Values{"key": {key}}, &resp); err != nil {
		return err
	}

	if flagJSON {
		return output.JSON(resp)
	}

	if len(resp.Versions) == 0 && len(resp.DeleteMarkers) == 0 {
		fmt.Println("No versions found.")
		return nil
	}

	tbl := output.NewTable("VERSION ID", "SIZE", "MODIFIED", "CURRENT")
	for _, v := range resp.Versions {
		modified := "-"
		if t, err := output.ParseTime(v.LastModified); err == nil {
			modified = output.HumanTime(t)
		}
		current := ""
		if v.IsLatest {
			current = output.Green("✓")
		}
		tbl.AddRow(v.VersionID, output.HumanSize(v.Size), modified, current)
	}
	for _, dm := range resp.DeleteMarkers {
		modified := "-"
		if t, err := output.ParseTime(dm.LastModified); err == nil {
			modified = output.HumanTime(t)
		}
		current := ""
		if dm.IsLatest {
			current = output.Red("✓")
		}
		tbl.AddRow(output.Red("[delete-marker]"), "-", modified, current)
	}
	tbl.Render()
	return nil
}
