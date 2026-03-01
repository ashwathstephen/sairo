package cmd

import (
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/client"
	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var (
	getRecursive bool
	getVersion   string
	getParallel  int
	getOverwrite bool
	getDryRun    bool
)

var getCmd = &cobra.Command{
	Use:   "get <bucket>/<key> [local-path]",
	Short: "Download an object to local filesystem",
	Long: `Download a file or recursively download a prefix.

Examples:
  sairo get my-bucket/reports/summary.pdf ./
  sairo get my-bucket/logs/2026-03/ ./logs/ -r
  sairo get my-bucket/config.yaml ./config.yaml --version abc123`,
	Args: cobra.RangeArgs(1, 2),
	RunE: runGet,
}

func init() {
	getCmd.Flags().BoolVarP(&getRecursive, "recursive", "r", false, "Download all objects under prefix")
	getCmd.Flags().StringVarP(&getVersion, "version", "v", "", "Download a specific version")
	getCmd.Flags().IntVar(&getParallel, "parallel", 4, "Concurrent downloads for recursive")
	getCmd.Flags().BoolVar(&getOverwrite, "overwrite", false, "Overwrite existing local files")
	getCmd.Flags().BoolVar(&getDryRun, "dry-run", false, "Show what would be downloaded")
	rootCmd.AddCommand(getCmd)
}

func runGet(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	bucket, key := parseBucketPath(args[0])
	localPath := "."
	if len(args) > 1 {
		localPath = args[1]
	}

	if getRecursive {
		return downloadRecursive(c, bucket, key, localPath)
	}
	return downloadSingle(c, bucket, key, localPath)
}

func downloadSingle(c *client.Client, bucket, key, localPath string) error {
	if key == "" {
		return fmt.Errorf("object key is required (use -r for recursive download)")
	}

	// Determine output file path
	destPath := localPath
	info, err := os.Stat(localPath)
	if err == nil && info.IsDir() {
		destPath = filepath.Join(localPath, filepath.Base(key))
	}

	if getDryRun {
		fmt.Printf("Would download: %s/%s → %s\n", bucket, key, destPath)
		return nil
	}

	// Check overwrite
	if !getOverwrite {
		if _, err := os.Stat(destPath); err == nil {
			return fmt.Errorf("file %s already exists (use --overwrite to replace)", destPath)
		}
	}

	// Get presigned URL
	params := url.Values{"key": {key}}
	if getVersion != "" {
		params.Set("version_id", getVersion)
		params.Set("expires", "3600")
	}

	endpoint := "/api/buckets/%s/presigned-url"
	if getVersion != "" {
		endpoint = "/api/buckets/%s/version-presigned-url"
	}

	var presigned struct {
		URL string `json:"url"`
	}
	if err := c.Get(fmt.Sprintf(endpoint, bucket), params, &presigned); err != nil {
		return err
	}

	// Download
	resp, err := http.Get(presigned.URL)
	if err != nil {
		return fmt.Errorf("download failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return fmt.Errorf("download failed: HTTP %d", resp.StatusCode)
	}

	// Create parent directory
	if dir := filepath.Dir(destPath); dir != "." {
		if err := os.MkdirAll(dir, 0755); err != nil {
			return err
		}
	}

	f, err := os.Create(destPath)
	if err != nil {
		return err
	}
	defer f.Close()

	var written int64
	if output.IsTTY() && resp.ContentLength > 0 {
		bar := output.NewProgressBar(resp.ContentLength, filepath.Base(key))
		written, err = io.Copy(io.MultiWriter(f, bar), resp.Body)
	} else {
		written, err = io.Copy(f, resp.Body)
	}
	if err != nil {
		return fmt.Errorf("download failed: %w", err)
	}

	if flagJSON {
		return output.JSON(map[string]interface{}{
			"key":   key,
			"path":  destPath,
			"bytes": written,
		})
	}

	output.Success("Downloaded %s (%s)", destPath, output.HumanSize(written))
	return nil
}

func downloadRecursive(c *client.Client, bucket, prefix, localDir string) error {
	// List all files
	params := url.Values{}
	if prefix != "" {
		params.Set("prefix", prefix)
	}

	resp, err := c.GetStream(fmt.Sprintf("/api/buckets/%s/list", bucket), params)
	if err != nil {
		return err
	}

	// Collect all files recursively
	var files []client.FileEntry
	err = collectFilesRecursive(c, bucket, prefix, resp, &files)
	if err != nil {
		return err
	}

	if len(files) == 0 {
		fmt.Println("No files to download.")
		return nil
	}

	var totalSize int64
	for _, f := range files {
		totalSize += f.Size
	}

	if getDryRun {
		fmt.Printf("Would download %d files (%s)\n", len(files), output.HumanSize(totalSize))
		for _, f := range files {
			fmt.Printf("  %s (%s)\n", f.Key, output.HumanSize(f.Size))
		}
		return nil
	}

	if !flagQuiet && output.IsTTY() {
		fmt.Printf("Downloading %d files (%s)...\n", len(files), output.HumanSize(totalSize))
	}

	// Download with parallelism
	sem := make(chan struct{}, getParallel)
	var wg sync.WaitGroup
	var errCount int64
	var doneCount int64

	for i, file := range files {
		sem <- struct{}{}
		wg.Add(1)
		go func(idx int, f client.FileEntry) {
			defer wg.Done()
			defer func() { <-sem }()

			// Compute relative path
			relPath := f.Key
			if prefix != "" && len(f.Key) > len(prefix) {
				relPath = f.Key[len(prefix):]
			}
			destPath := filepath.Join(localDir, relPath)

			if err := downloadFile(c, bucket, f.Key, destPath); err != nil {
				atomic.AddInt64(&errCount, 1)
				output.Error("[%d/%d] %s: %s", idx+1, len(files), f.Name, err)
			} else {
				done := atomic.AddInt64(&doneCount, 1)
				if output.IsTTY() && !flagQuiet {
					fmt.Printf("  [%d/%d] %s  %s  %s\n", done, len(files), f.Name, output.HumanSize(f.Size), output.Green("✓"))
				}
			}
		}(i, file)
	}
	wg.Wait()

	if errCount > 0 {
		return fmt.Errorf("%d files failed to download", errCount)
	}

	if flagJSON {
		return output.JSON(map[string]interface{}{
			"downloaded": len(files),
			"total_size": totalSize,
		})
	}

	output.Success("Downloaded %d files (%s)", len(files), output.HumanSize(totalSize))
	return nil
}

func collectFilesRecursive(c *client.Client, bucket, prefix string, resp *http.Response, files *[]client.FileEntry) error {
	folders, pageFiles, err := client.StreamListCollect(resp)
	if err != nil {
		return err
	}
	*files = append(*files, pageFiles...)

	for _, folder := range folders {
		params := url.Values{"prefix": {folder.Prefix}}
		subResp, err := c.GetStream(fmt.Sprintf("/api/buckets/%s/list", bucket), params)
		if err != nil {
			return err
		}
		if err := collectFilesRecursive(c, bucket, folder.Prefix, subResp, files); err != nil {
			return err
		}
	}
	return nil
}

func downloadFile(c *client.Client, bucket, key, destPath string) error {
	if !getOverwrite {
		if _, err := os.Stat(destPath); err == nil {
			return nil // skip existing
		}
	}

	params := url.Values{"key": {key}}
	var presigned struct {
		URL string `json:"url"`
	}
	if err := c.Get(fmt.Sprintf("/api/buckets/%s/presigned-url", bucket), params, &presigned); err != nil {
		return err
	}

	resp, err := http.Get(presigned.URL)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}

	if dir := filepath.Dir(destPath); dir != "." {
		if err := os.MkdirAll(dir, 0755); err != nil {
			return err
		}
	}

	f, err := os.Create(destPath)
	if err != nil {
		return err
	}
	defer f.Close()

	_, err = io.Copy(f, resp.Body)
	return err
}
