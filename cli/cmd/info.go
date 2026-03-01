package cmd

import (
	"fmt"
	"net/url"
	"strings"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var infoCmd = &cobra.Command{
	Use:   "info <bucket>/<key>",
	Short: "Show detailed metadata for an object",
	Long: `Display S3 HEAD metadata and columnar file schema (Parquet/ORC/Avro).

Examples:
  sairo info my-bucket/logs/app.log.gz
  sairo info my-bucket/events/data.parquet`,
	Args: cobra.ExactArgs(1),
	RunE: runInfo,
}

func init() {
	rootCmd.AddCommand(infoCmd)
}

type objectInfoResp struct {
	Key          string            `json:"key"`
	Size         int64             `json:"size"`
	ContentType  string            `json:"content_type"`
	ETag         string            `json:"etag"`
	LastModified string            `json:"last_modified"`
	Metadata     map[string]string `json:"metadata"`
	VersionID    string            `json:"version_id"`
	StorageClass string            `json:"storage_class"`
}

type fileMetadataResp struct {
	Format      string           `json:"format"`
	NumRows     int64            `json:"num_rows"`
	NumColumns  int              `json:"num_columns"`
	NumRowGroups int             `json:"num_row_groups"`
	NumStripes  int              `json:"num_stripes"`
	CreatedBy   string           `json:"created_by"`
	Compression string           `json:"compression"`
	Columns     []columnInfo     `json:"columns"`
	FileSize    int64            `json:"file_size"`
	SchemaName  string           `json:"schema_name"`
	Namespace   string           `json:"namespace"`
}

type columnInfo struct {
	Name     string `json:"name"`
	Type     string `json:"type"`
	Nullable bool   `json:"nullable"`
}

func runInfo(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	bucket, key := parseBucketPath(args[0])
	if key == "" {
		return fmt.Errorf("object key is required (e.g., my-bucket/path/to/file)")
	}

	params := url.Values{}
	params.Set("key", key)

	var info objectInfoResp
	if err := c.Get(fmt.Sprintf("/api/buckets/%s/object-info", bucket), params, &info); err != nil {
		return err
	}

	// Try to get file metadata for columnar files
	var meta *fileMetadataResp
	ext := strings.ToLower(key)
	if strings.HasSuffix(ext, ".parquet") || strings.HasSuffix(ext, ".orc") || strings.HasSuffix(ext, ".avro") {
		var m fileMetadataResp
		if err := c.Get(fmt.Sprintf("/api/buckets/%s/file-metadata", bucket), params, &m); err == nil {
			meta = &m
		}
	}

	if flagJSON {
		result := map[string]interface{}{"object": info}
		if meta != nil {
			result["schema"] = meta
		}
		return output.JSON(result)
	}

	fmt.Println()
	output.Info("Key", info.Key)
	if info.Size >= 0 {
		output.Info("Size", fmt.Sprintf("%s (%s bytes)", output.HumanSize(info.Size), output.FormatNumber(info.Size)))
	}
	if info.ContentType != "" {
		output.Info("Content-Type", info.ContentType)
	}
	if info.ETag != "" {
		output.Info("ETag", info.ETag)
	}
	if info.LastModified != "" {
		modified := info.LastModified
		if t, err := output.ParseTime(info.LastModified); err == nil {
			modified = fmt.Sprintf("%s (%s)", info.LastModified, output.HumanTime(t))
		}
		output.Info("Last Modified", modified)
	}
	if info.StorageClass != "" {
		output.Info("Storage Class", info.StorageClass)
	}
	if info.VersionID != "" {
		output.Info("Version ID", info.VersionID)
	}
	for k, v := range info.Metadata {
		output.Info("Meta:"+k, v)
	}

	// Columnar schema
	if meta != nil {
		fmt.Println()
		output.Info("Format", meta.Format)
		if meta.NumRows > 0 {
			output.Info("Rows", output.FormatNumber(meta.NumRows))
		}
		output.Info("Columns", fmt.Sprintf("%d", meta.NumColumns))
		if meta.NumRowGroups > 0 {
			output.Info("Row Groups", fmt.Sprintf("%d", meta.NumRowGroups))
		}
		if meta.NumStripes > 0 {
			output.Info("Stripes", fmt.Sprintf("%d", meta.NumStripes))
		}
		if meta.CreatedBy != "" {
			output.Info("Created By", meta.CreatedBy)
		}
		if meta.Compression != "" {
			output.Info("Compression", meta.Compression)
		}

		if len(meta.Columns) > 0 {
			fmt.Println()
			tbl := output.NewTable("COLUMN", "TYPE", "NULLABLE")
			for _, col := range meta.Columns {
				nullable := "yes"
				if !col.Nullable {
					nullable = "no"
				}
				tbl.AddRow(col.Name, col.Type, nullable)
			}
			tbl.Render()
		}
	}

	fmt.Println()
	return nil
}
