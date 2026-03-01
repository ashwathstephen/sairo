package cmd

import (
	"fmt"
	"net/url"
	"strings"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var tagsCmd = &cobra.Command{
	Use:   "tags <bucket>[/<key>] [get|set|delete] [key=value ...]",
	Short: "View or manage bucket or object tags",
	Long: `Manage tags on buckets or individual objects.

Examples:
  sairo tags my-bucket
  sairo tags my-bucket set env=prod team=platform
  sairo tags my-bucket/file.txt
  sairo tags my-bucket/file.txt set owner=alice
  sairo tags my-bucket/file.txt delete`,
	Args: cobra.MinimumNArgs(1),
	RunE: runTags,
}

func init() {
	rootCmd.AddCommand(tagsCmd)
}

type tagsResp struct {
	Tags map[string]string `json:"tags"`
}

func runTags(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	bucket, key := parseBucketPath(args[0])
	isObject := key != ""

	action := "get"
	tagsStartIdx := 1
	if len(args) > 1 {
		switch strings.ToLower(args[1]) {
		case "get":
			action = "get"
			tagsStartIdx = 2
		case "set":
			action = "set"
			tagsStartIdx = 2
		case "delete":
			action = "delete"
			tagsStartIdx = 2
		default:
			// Could be a key=value pair for implicit "set"
			if strings.Contains(args[1], "=") {
				action = "set"
				tagsStartIdx = 1
			}
		}
	}

	switch action {
	case "get":
		var resp tagsResp
		var endpoint string
		var params url.Values
		if isObject {
			endpoint = fmt.Sprintf("/api/buckets/%s/object-tagging", bucket)
			params = url.Values{"key": {key}}
		} else {
			endpoint = fmt.Sprintf("/api/buckets/%s/tagging", bucket)
		}
		if err := c.Get(endpoint, params, &resp); err != nil {
			return err
		}
		if flagJSON {
			return output.JSON(resp)
		}
		if len(resp.Tags) == 0 {
			fmt.Println("No tags set.")
			return nil
		}
		for k, v := range resp.Tags {
			fmt.Printf("  %s=%s\n", k, v)
		}
		return nil

	case "set":
		tags := make(map[string]string)
		for _, arg := range args[tagsStartIdx:] {
			parts := strings.SplitN(arg, "=", 2)
			if len(parts) != 2 {
				return fmt.Errorf("invalid tag format: %s (expected key=value)", arg)
			}
			tags[parts[0]] = parts[1]
		}
		if len(tags) == 0 {
			return fmt.Errorf("at least one key=value pair is required")
		}

		payload := map[string]interface{}{"tags": tags}
		var resp map[string]interface{}
		var endpoint string
		if isObject {
			endpoint = fmt.Sprintf("/api/buckets/%s/object-tagging?key=%s", bucket, url.QueryEscape(key))
		} else {
			endpoint = fmt.Sprintf("/api/buckets/%s/tagging", bucket)
		}
		if err := c.Put(endpoint, payload, &resp); err != nil {
			return err
		}
		if flagJSON {
			return output.JSON(resp)
		}
		output.Success("Tags updated")
		return nil

	case "delete":
		if !isObject {
			return fmt.Errorf("delete is only supported for object tags")
		}
		var resp map[string]interface{}
		endpoint := fmt.Sprintf("/api/buckets/%s/object-tagging?key=%s", bucket, url.QueryEscape(key))
		if err := c.Delete(endpoint, &resp); err != nil {
			return err
		}
		if flagJSON {
			return output.JSON(resp)
		}
		output.Success("Tags deleted")
		return nil

	default:
		return fmt.Errorf("unknown action: %s (use get, set, or delete)", action)
	}
}
